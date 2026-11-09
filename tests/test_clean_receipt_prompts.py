"""No ForceReply composer, safe unambiguous receipt selection and Rich invoices."""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas import receipt_intents
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.file_sales import manual_handlers as files
from telegram_membership_saas.modules.owner_notifications import _invoice_rich_html
from telegram_membership_saas.modules.telegram_commerce import handlers as subs
from telegram_membership_saas.modules.telegram_commerce.service import (
    ReviewItem,
    SubmissionResult,
    encode_uuid,
)
from telegram_membership_saas.receipt_prompt_ui import (
    receipt_prompt,
    send_receipt_prompt,
)


@pytest.fixture(autouse=True)
def clear_receipt_selection():
    receipt_intents._selected.clear()
    yield
    receipt_intents._selected.clear()


def _bot():
    bot = MagicMock()
    bot.id = 8000
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_document = AsyncMock()
    bot.copy_message = AsyncMock(return_value=SimpleNamespace(message_id=365))
    return bot


def _upload(*, buyer=123, mime_type=None, photo=True, reply=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=buyer),
        chat=SimpleNamespace(id=buyer, type="private"),
        reply_to_message=reply,
        message_id=555, text=None,
        photo=[SimpleNamespace(file_id="photo-id")] if photo else None,
        document=(SimpleNamespace(file_id="document-id", mime_type=mime_type)
                  if mime_type else None),
    )


@asynccontextmanager
async def _scope(*_args, **_kwargs):
    yield MagicMock()


def _handler(router, name, *, callback=False):
    observer = router.callback_query if callback else router.message
    return next(x.callback for x in observer.handlers if x.callback.__name__ == name)


def _callback(payload):
    return SimpleNamespace(
        data=payload,
        from_user=SimpleNamespace(id=123, username="buyer", first_name="Buyer"),
        message=SimpleNamespace(chat=SimpleNamespace(id=123, type="private")),
        answer=AsyncMock(),
    )


def test_cross_type_receipt_selection_and_stale_stop(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(receipt_intents, "monotonic", lambda: clock[0])
    sub_id, file_id = uuid4(), uuid4()
    receipt_intents.arm(8000, 123, kind="subscription", request_id=sub_id)
    assert receipt_intents.peek(8000, 123, kind="subscription") == sub_id
    assert receipt_intents.peek(8001, 123, kind="subscription") is None
    assert receipt_intents.peek(8000, 124, kind="subscription") is None

    receipt_intents.arm(8000, 123, kind="file", request_id=file_id)
    assert receipt_intents.peek(8000, 123, kind="subscription") is None
    assert not receipt_intents.clear(
        8000, 123, kind="subscription", request_id=sub_id,
    )
    assert receipt_intents.peek(8000, 123, kind="file") == file_id
    assert receipt_intents.consume(8000, 123, kind="file") == file_id
    assert receipt_intents.consume(8000, 123, kind="file") is None

    receipt_intents.arm(8000, 123, kind="file", request_id=file_id)
    clock[0] += 30 * 60 + 1
    assert receipt_intents.peek(8000, 123, kind="file") is None


@pytest.mark.parametrize(("lang", "required"), [
    ("ru", "Нажимать «Ответить» не нужно"),
    ("en", "No need to use Reply"),
])
def test_receipt_prompt_uses_rich_table_and_hides_raw_ids(lang, required):
    ident = uuid4()
    token = _language.set(lang)
    try:
        rich, classic = receipt_prompt(
            kind="file", request_id=ident,
            name="<Premium> & Video", amount="100", currency="RUB",
        )
    finally:
        _language.reset(token)
    assert required in rich
    assert "<table bordered compact>" in rich
    assert "&lt;Premium&gt; &amp; Video" in rich
    assert "100 RUB" in rich
    assert ident.hex not in rich
    assert str(ident) not in rich
    assert "FILE_PAYMENT_RECEIPT:" not in rich + classic
    assert "ForceReply" not in rich + classic
    assert ident.hex[:10].upper() in rich


async def test_receipt_instruction_has_no_force_reply_markup(monkeypatch):
    send = AsyncMock()
    from telegram_membership_saas import receipt_prompt_ui

    monkeypatch.setattr(receipt_prompt_ui, "send_payment_review", send)
    ident = uuid4()
    await send_receipt_prompt(
        _bot(), buyer_id=123, kind="subscription", request_id=ident,
        cancel_data="receiptstop:" + encode_uuid(ident),
    )
    opts = send.await_args.kwargs
    assert opts["chat_id"] == 123
    assert "reply_parameters" not in opts
    assert "ForceReply" not in repr(opts["reply_markup"])
    assert opts["reply_markup"].inline_keyboard[0][0].callback_data == (
        "receiptstop:" + encode_uuid(ident)
    )
    assert str(ident) not in opts["rich_html"]


async def test_subscription_direct_upload_is_only_selected_target(monkeypatch):
    project, ident = uuid4(), uuid4()
    bot = _bot()
    receipt_intents.arm(bot.id, 123, kind="subscription", request_id=ident)
    router = subs.create_telegram_commerce_router(MagicMock(), project_id=project)
    upload = _upload(photo=False, mime_type="application/pdf")
    own_filter = subs.ActiveSubscriptionReceiptFilter()
    other_filter = files.ActiveFileReceiptFilter()
    assert await own_filter(upload, bot)
    assert not await other_filter(upload, bot)
    assert not await own_filter(_upload(buyer=124), bot)
    assert not await own_filter(_upload(reply=SimpleNamespace()), bot)

    submission = SubmissionResult(
        request=SimpleNamespace(id=ident),
        item=ReviewItem(
            request_id=ident, customer_telegram_user_id=123,
            customer_username="buyer", plan_name="Premium",
            amount=Decimal("500"), currency_code="RUB",
            payment_method_name="Ozon", status="waiting_review",
        ),
        reviewer_telegram_ids=(456,), newly_submitted=True,
    )
    submit = AsyncMock(return_value=submission)
    monkeypatch.setattr(subs, "system_project_session_scope", _scope)
    monkeypatch.setattr(subs, "recipient_language", _scope)
    monkeypatch.setattr(subs, "submit_checkout_for_telegram_user", submit)
    await _handler(router, "direct_receipt_handler")(upload, bot)
    submit.assert_awaited_once()
    assert submit.await_args.kwargs["project_id"] == project
    assert submit.await_args.kwargs["request_id"] == ident
    assert submit.await_args.kwargs["telegram_user_id"] == 123
    bot.copy_message.assert_awaited_once_with(
        chat_id=456, from_chat_id=123, message_id=555,
    )
    assert receipt_intents.peek(bot.id, 123, kind="subscription") is None
    # One-time consume: a second upload must not trigger the same review.
    assert not await own_filter(upload, bot)


async def test_file_direct_upload_withdraws_one_target_and_keeps_receipt(monkeypatch):
    project, order_id = uuid4(), uuid4()
    bot = _bot()
    order = SimpleNamespace(
        id=order_id, status="submitted", telegram_user_id=123,
        product_name="Premium file", amount=Decimal("100"),
        currency_code="RUB", payment_method_name="Ozon",
        receipt_kind="photo", receipt_file_id="file-id",
    )
    submit = AsyncMock(return_value=(order, (456,)))
    monkeypatch.setattr(files, "submit_manual_receipt", submit)
    monkeypatch.setattr(files, "recipient_language", _scope)
    receipt_intents.arm(bot.id, 123, kind="file", request_id=order_id)
    router = files.create_manual_file_router(MagicMock(), project_id=project)
    upload = _upload()
    assert await files.ActiveFileReceiptFilter()(upload, bot)
    assert not await subs.ActiveSubscriptionReceiptFilter()(upload, bot)

    await _handler(router, "direct_file_receipt")(upload, bot)
    submit.assert_awaited_once()
    assert submit.await_args.kwargs["order_id"] == order_id
    assert submit.await_args.kwargs["buyer_id"] == 123
    assert submit.await_args.kwargs["project_id"] == project
    bot.send_photo.assert_awaited_once_with(456, "file-id")
    assert receipt_intents.peek(bot.id, 123, kind="file") is None


async def test_invalid_direct_receipt_keeps_selection_and_no_submission(monkeypatch):
    ident = uuid4()
    receipt_intents.arm(8000, 123, kind="file", request_id=ident)
    send = AsyncMock()
    monkeypatch.setattr(files, "submit_manual_receipt", send)
    router = files.create_manual_file_router(MagicMock(), project_id=uuid4())
    bad = _upload(photo=False, mime_type="application/zip")
    await _handler(router, "direct_file_receipt")(bad, _bot())
    send.assert_not_awaited()
    assert receipt_intents.peek(8000, 123, kind="file") == ident


async def test_file_paid_selects_normal_upload_without_force_reply(monkeypatch):
    ident, project = uuid4(), uuid4()
    order = SimpleNamespace(
        id=ident, status="pending", amount=Decimal("100"),
        currency_code="RUB", product_name="Sample <File>",
    )
    monkeypatch.setattr(files, "require_buyer_order", AsyncMock(return_value=order))
    prompt = AsyncMock()
    monkeypatch.setattr(files, "send_receipt_prompt", prompt)
    bot = _bot()
    cb = _callback("fm:paid:" + encode_uuid(ident))
    router = files.create_manual_file_router(MagicMock(), project_id=project)
    await _handler(router, "file_manual_callback", callback=True)(cb, bot)
    prompt.assert_awaited_once()
    assert prompt.await_args.kwargs["name"] == order.product_name
    assert prompt.await_args.kwargs["cancel_data"] == (
        "fm:receiptstop:" + encode_uuid(ident)
    )
    assert receipt_intents.peek(bot.id, 123, kind="file") == ident
    cb.answer.assert_awaited_once()


def test_owner_unpaid_invoice_is_rich_escaped_and_preserves_method():
    data = {
        "request_id": uuid4().hex, "customer_id": uuid4().hex,
        "telegram_id": 8372688203, "username": "buyer<script>",
        "first_name": "Buyer & Friends", "plan_name": "Premium <VIP>",
        "duration_value": 1, "duration_unit": "month",
        "amount": "500", "currency": "RUB", "method": "Ozon & Bank",
    }
    for lang, title, waiting in (
        ("ru", "Счёт сформирован", "Ожидает оплату"),
        ("en", "Payment invoice created", "Awaiting payment"),
    ):
        token = _language.set(lang)
        try:
            html = _invoice_rich_html(data)
        finally:
            _language.reset(token)
        assert "<table bordered compact>" in html
        assert title in html and waiting in html
        assert "500 RUB" in html
        assert "Ozon &amp; Bank" in html
        assert "Premium &lt;VIP&gt;" in html
        assert "&lt;script&gt;" in html and "&amp; Friends" in html
        assert "8372688203" in html
        assert "Payment approved" not in html


async def test_invalid_old_subscription_reply_cannot_switch_selected_order(
    monkeypatch,
):
    old_id, selected_id = uuid4(), uuid4()
    bot = _bot()
    receipt_intents.arm(bot.id, 123, kind="subscription", request_id=selected_id)
    submit = AsyncMock()
    monkeypatch.setattr(subs, "submit_checkout_for_telegram_user", submit)
    legacy = _upload(photo=False, mime_type="application/zip", reply=SimpleNamespace(
        from_user=SimpleNamespace(id=bot.id),
        text=subs.RECEIPT_REPLY_PREFIX + str(old_id) + "\nAttach receipt",
    ))
    router = subs.create_telegram_commerce_router(
        MagicMock(), project_id=uuid4(),
    )
    await _handler(router, "receipt_handler")(legacy, bot)
    submit.assert_not_awaited()
    assert receipt_intents.peek(bot.id, 123, kind="subscription") == selected_id


async def test_invalid_old_file_reply_cannot_switch_selected_order(monkeypatch):
    old_id, selected_id = uuid4(), uuid4()
    bot = _bot()
    receipt_intents.arm(bot.id, 123, kind="file", request_id=selected_id)
    submit = AsyncMock()
    monkeypatch.setattr(files, "submit_manual_receipt", submit)
    legacy = _upload(photo=False, mime_type="application/zip", reply=SimpleNamespace(
        from_user=SimpleNamespace(id=bot.id),
        text=files.RECEIPT_PREFIX + str(old_id),
    ))
    router = files.create_manual_file_router(MagicMock(), project_id=uuid4())
    await _handler(router, "manual_receipt")(legacy, bot)
    submit.assert_not_awaited()
    assert receipt_intents.peek(bot.id, 123, kind="file") == selected_id


async def test_submitted_subscription_receipt_has_explicit_retry_button(
    monkeypatch,
):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import CopyMessage

    project, ident = uuid4(), uuid4()
    bot = _bot()
    receipt_intents.arm(bot.id, 123, kind="subscription", request_id=ident)
    submission = SubmissionResult(
        request=SimpleNamespace(id=ident),
        item=ReviewItem(
            request_id=ident, customer_telegram_user_id=123,
            customer_username="buyer", plan_name="Premium",
            amount=Decimal("500"), currency_code="RUB",
            payment_method_name="Ozon", status="waiting_review",
        ),
        reviewer_telegram_ids=(456,), newly_submitted=True,
    )
    monkeypatch.setattr(subs, "system_project_session_scope", _scope)
    monkeypatch.setattr(subs, "submit_checkout_for_telegram_user", AsyncMock(
        return_value=submission,
    ))
    bot.copy_message.side_effect = TelegramBadRequest(
        method=CopyMessage(chat_id=456, from_chat_id=123, message_id=555),
        message="Bad Request: recipient not reachable",
    )
    router = subs.create_telegram_commerce_router(MagicMock(), project_id=project)
    await _handler(router, "direct_receipt_handler")(_upload(), bot)
    assert receipt_intents.peek(bot.id, 123, kind="subscription") is None
    retry = bot.send_message.await_args.kwargs["reply_markup"]
    assert retry.inline_keyboard[0][0].callback_data == (
        "receiptretry:" + encode_uuid(ident)
    )
    assert "Повторить чек" in bot.send_message.await_args.kwargs["text"]


async def test_submitted_receipt_retry_rechecks_buyer_status(monkeypatch):
    project, ident = uuid4(), uuid4()
    bot = _bot()
    gate = AsyncMock()
    prompt = AsyncMock()
    monkeypatch.setattr(subs, "system_project_session_scope", _scope)
    monkeypatch.setattr(subs, "require_awaiting_receipt_for_telegram_user", gate)
    monkeypatch.setattr(subs, "send_receipt_prompt", prompt)
    router = subs.create_telegram_commerce_router(MagicMock(), project_id=project)
    cb = _callback("receiptretry:" + encode_uuid(ident))
    await _handler(router, "retry_receipt_handler", callback=True)(cb, bot)
    gate.assert_awaited_once()
    assert gate.await_args.kwargs["retry_submitted"] is True
    assert gate.await_args.kwargs["request_id"] == ident
    assert gate.await_args.kwargs["telegram_user_id"] == 123
    prompt.assert_awaited_once()
    assert receipt_intents.peek(bot.id, 123, kind="subscription") == ident

    receipt_intents.clear(bot.id, 123, kind="subscription", request_id=ident)
    gate.side_effect = ValueError("Request reviewed")
    prompt.reset_mock()
    denied = _callback("receiptretry:" + encode_uuid(ident))
    await _handler(router, "retry_receipt_handler", callback=True)(denied, bot)
    prompt.assert_not_awaited()
    assert denied.answer.await_args.kwargs["show_alert"] is True
    assert receipt_intents.peek(bot.id, 123, kind="subscription") is None
