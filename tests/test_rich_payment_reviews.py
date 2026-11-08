"""Payment review Rich cards are presentation-only, scoped and receipt-linked."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
)

from telegram_membership_saas import payment_review_ui as ui
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.file_sales import manual_handlers as files
from telegram_membership_saas.modules.telegram_commerce import handlers as commerce
from telegram_membership_saas.modules.telegram_commerce.service import (
    ReviewItem,
    ReviewResult,
    encode_uuid,
)



def test_rich_review_imports_in_fresh_process_before_commerce_handlers():
    proc = subprocess.run(
        [sys.executable, "-c",
         "from telegram_membership_saas.payment_review_ui import "
         "rich_subscription_review; print('ok')"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def _review():
    return ReviewItem(
        request_id=uuid4(), customer_telegram_user_id=43212345,
        customer_username="<buyer> & friend", plan_name="VIP <plan> & more",
        amount=Decimal("550.000000000000000000"), currency_code="RUB",
        payment_method_name="Ozon <Банк> & карты",
        status="waiting_review",
    )


def _file_order(status="submitted"):
    return SimpleNamespace(
        id=uuid4(), telegram_user_id=13245567,
        product_name="Моя <жопа> & видео",
        amount=Decimal("100.000000000000000000"),
        currency_code="RUB", payment_method_name="СберБанк <VIP>",
        status=status,
        receipt_kind="photo" if status == "submitted" else None,
        receipt_file_id="tg-receipt-id" if status == "submitted" else None,
    )


@pytest.mark.parametrize(("language", "heading", "warning"), [
    ("ru", "Заявка на проверку", "проверьте поступление денег"),
    ("en", "Payment review", "Verify funds in your bank"),
])
def test_subscription_rich_review_escapes_and_never_converts_payment(
    language, heading, warning,
):
    token = _language.set(language)
    try:
        html = ui.rich_subscription_review(_review(), receipt_attached=True)
        history = ui.rich_subscription_review(_review(), receipt_attached=False)
    finally:
        _language.reset(token)
    assert heading in html and warning in html
    assert "550 RUB" in html
    assert "VIP &lt;plan&gt; &amp; more" in html
    assert "@&lt;buyer&gt; &amp; friend" in html
    assert "Ozon &lt;Банк&gt; &amp; карты" in html
    assert "<buyer>" not in html
    assert html.count("<table bordered compact>") == 1
    assert "43212345" in html
    assert "above" in html if language == "en" else "выше" in html
    assert "history" in history if language == "en" else "истории" in history


@pytest.mark.parametrize(("language", "approved", "title"), [
    ("ru", True, "Оплата подтверждена"),
    ("en", True, "Payment approved"),
    ("ru", False, "Оплата отклонена"),
    ("en", False, "Payment rejected"),
])
def test_subscription_result_rich_keeps_committed_status_and_local_dates(
    language, approved, title,
):
    outcome = ReviewResult(
        item=_review(), changed=True,
        subscription_period_end=datetime(2026, 11, 9, 23, 30, tzinfo=UTC),
        project_timezone="Europe/Moscow",
    )
    token = _language.set(language)
    try:
        html = ui.rich_subscription_review_result(outcome, approve=approved)
    finally:
        _language.reset(token)
    assert title in html
    assert "550 RUB" in html and "VIP &lt;plan&gt; &amp; more" in html
    if approved:
        assert "10.11.2026 02:30 (Europe/Moscow)" in html
    else:
        assert "10.11.2026" not in html


def test_subscription_approval_lifetime_and_empty_username():
    item = _review()
    from dataclasses import replace
    no_name = replace(item, customer_username=None)
    outcome = ReviewResult(item=no_name, changed=True)
    html = ui.rich_subscription_review_result(outcome, approve=True)
    assert "Бессрочно" in html and str(item.customer_telegram_user_id) in html
    assert "@" + str(item.customer_telegram_user_id) not in html


@pytest.mark.parametrize(("language", "label", "status"), [
    ("ru", "Проверка покупки файла", "На проверке"),
    ("en", "File purchase review", "Under review"),
])
def test_file_review_and_queue_are_rich_safe_and_keep_status(
    language, label, status,
):
    order = _file_order()
    pending = _file_order("pending")
    token = _language.set(language)
    try:
        card = ui.rich_file_review(order, receipt_attached=True)
        waiting = ui.rich_file_review(pending)
        queue = ui.rich_file_reviews_queue((order, pending), page=1, total=13)
    finally:
        _language.reset(token)
    assert label in card and status in card
    assert "Моя &lt;жопа&gt; &amp; видео" in card
    assert "СберБанк &lt;VIP&gt;" in card
    assert "100 RUB" in card and "100.000000" not in card
    assert "<жопа>" not in card
    assert "receipt" in card.lower() if language == "en" else "чек" in card.lower()
    assert "💰" in card and "🏷" in card
    assert "Не подтверждайте" in waiting if language == "ru" else (
        "Do not approve" in waiting
    )
    assert queue.count("<tr>") == 3 and "13" in queue
    assert "2 / 2" in queue and "100 RUB" in queue


@pytest.mark.parametrize(("approve", "label"), [
    (True, "Оплата файла подтверждена"),
    (False, "Оплата файла отклонена"),
])
def test_file_review_result_and_close_warning(approve, label):
    order = _file_order()
    result = ui.rich_file_review_result(order, approve=approve)
    warning = ui.rich_file_close_warning(order)
    assert label in result
    assert "100 RUB" in result and "13245567" in result
    assert "Моя &lt;жопа&gt;" in result
    assert "Закрытие неоплаченного" in warning
    assert "СберБанк &lt;VIP&gt;" in warning
    assert "<blockquote>" in warning


def _bot():
    bot = MagicMock()
    bot.id = 777
    bot.send_rich_message = AsyncMock()
    bot.send_message = AsyncMock()
    bot.copy_message = AsyncMock(return_value=SimpleNamespace(message_id=925))
    return bot


def _keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Approve", style="success", callback_data="approve:yes",
        ),
        InlineKeyboardButton(
            text="❌ Reject", style="danger", callback_data="reject:no",
        ),
    ]])


async def test_rich_alert_keeps_receipt_threading_and_exact_buttons(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    monkeypatch.setattr(
        ui, "InputRichMessage", lambda **kwargs: SimpleNamespace(**kwargs),
    )
    bot = _bot()
    markup = _keyboard()
    reply = ReplyParameters(message_id=925)
    await ui.send_payment_review(
        bot, chat_id=1234, rich_html="<h3>Rich</h3>",
        legacy_text="plain", reply_markup=markup, reply_parameters=reply,
    )
    bot.send_rich_message.assert_awaited_once()
    options = bot.send_rich_message.await_args.kwargs
    assert options["chat_id"] == 1234
    assert options["reply_parameters"] is reply
    assert options["reply_markup"] is markup
    assert options["rich_message"].html == "<h3>Rich</h3>"
    bot.send_message.assert_not_awaited()


async def test_rich_rejection_falls_back_once_with_same_reply_and_keyboard(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    monkeypatch.setattr(
        ui, "InputRichMessage", lambda **kwargs: SimpleNamespace(**kwargs),
    )
    bot = _bot()
    bot.send_rich_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=1234, text="ignored"),
        message="Bad Request: can't parse rich message",
    )
    markup = _keyboard()
    reply = ReplyParameters(message_id=925)
    await ui.send_payment_review(
        bot, chat_id=1234, rich_html="<bad>",
        legacy_text="<b>Original receipt</b>",
        parse_mode="HTML", reply_markup=markup, reply_parameters=reply,
    )
    bot.send_message.assert_awaited_once_with(
        chat_id=1234, text="<b>Original receipt</b>",
        parse_mode="HTML", reply_markup=markup, reply_parameters=reply,
    )


async def test_master_switch_off_and_old_aiogram_keep_classic(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: False)
    bot = _bot()
    markup = _keyboard()
    await ui.send_payment_review(
        bot, chat_id=1, rich_html="<h3>Rich</h3>",
        legacy_text="Classic", reply_markup=markup,
    )
    bot.send_message.assert_awaited_once_with(
        chat_id=1, text="Classic", reply_markup=markup,
    )
    bot.send_rich_message.assert_not_awaited()
    bot.send_message.reset_mock()
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    monkeypatch.setattr(ui, "InputRichMessage", None)
    await ui.send_payment_review(
        bot, chat_id=1, rich_html="<h3>Rich</h3>",
        legacy_text="Classic", reply_markup=markup,
    )
    bot.send_message.assert_awaited_once()


async def test_transport_error_does_not_duplicate_payment_review(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    monkeypatch.setattr(
        ui, "InputRichMessage", lambda **kwargs: SimpleNamespace(**kwargs),
    )
    bot = _bot()
    bot.send_rich_message.side_effect = ConnectionError("timeout after delivery")
    with pytest.raises(ConnectionError):
        await ui.send_payment_review(
            bot, chat_id=1234, rich_html="<h3>Rich</h3>",
            legacy_text="Classic", reply_markup=_keyboard(),
        )
    bot.send_message.assert_not_awaited()


async def test_unrelated_telegram_bad_request_is_not_retried(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    monkeypatch.setattr(
        ui, "InputRichMessage", lambda **kwargs: SimpleNamespace(**kwargs),
    )
    bot = _bot()
    bot.send_rich_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=1, text="bad"),
        message="Bad Request: chat not found",
    )
    with pytest.raises(TelegramBadRequest):
        await ui.send_payment_review(
            bot, chat_id=1, rich_html="<h3>Rich</h3>",
            legacy_text="Classic",
        )
    bot.send_message.assert_not_awaited()


@asynccontextmanager
async def _scope(*args, **kwargs):
    yield MagicMock()


async def test_subscription_receipt_is_copied_before_rich_and_linked(monkeypatch):
    from telegram_membership_saas.modules.telegram_commerce.service import (
        SubmissionResult,
    )

    project, request_id = uuid4(), uuid4()
    item = _review()
    item = __import__("dataclasses").replace(
        item, request_id=request_id, customer_telegram_user_id=100,
    )
    monkeypatch.setattr(commerce, "system_project_session_scope", _scope)
    monkeypatch.setattr(commerce, "recipient_language", _scope)
    monkeypatch.setattr(commerce, "submit_checkout_for_telegram_user", AsyncMock(
        return_value=SubmissionResult(
            request=SimpleNamespace(id=request_id), item=item,
            reviewer_telegram_ids=(200,), newly_submitted=True,
        ),
    ))
    sender = AsyncMock()
    monkeypatch.setattr(commerce, "send_payment_review", sender)
    bot = _bot()
    original = SimpleNamespace(
        from_user=SimpleNamespace(id=bot.id),
        text=commerce.RECEIPT_REPLY_PREFIX + str(request_id),
    )
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=100), chat=SimpleNamespace(type="private", id=100),
        reply_to_message=original, message_id=30,
        photo=[SimpleNamespace(file_id="proof")], document=None, text=None,
    )
    router = commerce.create_telegram_commerce_router(MagicMock(), project_id=project)
    callback = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "receipt_handler"
    )
    await callback(msg, bot)
    bot.copy_message.assert_awaited_once_with(
        chat_id=200, from_chat_id=100, message_id=30,
    )
    sender.assert_awaited_once()
    opts = sender.await_args.kwargs
    assert opts["chat_id"] == 200
    assert opts["reply_parameters"].message_id == 925
    assert "100" in opts["rich_html"]
    actions = [
        b.callback_data for row in opts["reply_markup"].inline_keyboard for b in row
    ]
    assert "approve:" + encode_uuid(request_id) in actions
    assert "reject:" + encode_uuid(request_id) in actions
    assert "own:invoice:" + encode_uuid(request_id) in actions


async def test_file_review_detail_stays_scoped_and_reuses_exact_actions(monkeypatch):
    project, order = uuid4(), _file_order()
    monkeypatch.setattr(files, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    loader = AsyncMock(return_value=order)
    monkeypatch.setattr(files, "load_manual_review", loader)
    render = AsyncMock()
    monkeypatch.setattr(files, "send_payment_review", render)
    bot = _bot()
    bot.send_photo = AsyncMock()
    bot.send_document = AsyncMock()
    query = SimpleNamespace(
        data="fm:review:" + encode_uuid(order.id),
        from_user=SimpleNamespace(
            id=123, username="admin", first_name="Admin",
        ),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=123),
        ),
        answer=AsyncMock(),
    )
    router = files.create_manual_file_router(MagicMock(), project_id=project)
    handler = next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "file_manual_callback"
    )
    await handler(query, bot)
    loader.assert_awaited_once()
    assert loader.await_args.kwargs["project_id"] == project
    assert loader.await_args.kwargs["order_id"] == order.id
    bot.send_photo.assert_awaited_once_with(123, "tg-receipt-id")
    render.assert_awaited_once()
    options = render.await_args.kwargs
    assert "100 RUB" in options["rich_html"]
    callbacks = [
        b.callback_data
        for row in options["reply_markup"].inline_keyboard for b in row
    ]
    assert callbacks == [
        "fm:approve:" + encode_uuid(order.id),
        "fm:reject:" + encode_uuid(order.id),
    ]
    assert options["chat_id"] == 123
