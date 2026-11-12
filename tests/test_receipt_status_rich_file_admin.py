"""Paid receipt controls finish on the exact card; file admin screens stay read-only."""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup

from telegram_membership_saas import receipt_intents
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.file_sales import handlers as file_admin
from telegram_membership_saas.modules.file_sales import manual_handlers as file_manual
from telegram_membership_saas.modules.file_sales import rich_ui
from telegram_membership_saas.modules.telegram_commerce import handlers as subs
from telegram_membership_saas.modules.telegram_commerce.service import (
    ReviewItem,
    SubmissionResult,
)
from telegram_membership_saas.receipt_prompt_ui import (
    send_receipt_prompt,
    update_receipt_prompt,
)


@pytest.fixture(autouse=True)
def reset_receipts():
    receipt_intents._selected.clear()
    yield
    receipt_intents._selected.clear()


def _bot():
    bot = MagicMock()
    bot.id = 442
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=901))
    bot.send_rich_message = AsyncMock(return_value=SimpleNamespace(message_id=901))
    bot.edit_message_reply_markup = AsyncMock()
    bot.copy_message = AsyncMock(return_value=SimpleNamespace(message_id=922))
    bot.send_photo = AsyncMock()
    bot.send_document = AsyncMock()
    return bot


def _message(buyer=123, *, photo=True):
    return SimpleNamespace(
        chat=SimpleNamespace(id=buyer, type="private"),
        from_user=SimpleNamespace(id=buyer),
        message_id=50,
        text=None,
        reply_to_message=None,
        photo=[SimpleNamespace(file_id="receipt")] if photo else None,
        document=None,
    )


def _callback(action):
    return SimpleNamespace(
        data=action,
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(id=123, type="private"),
                                message_id=901),
        answer=AsyncMock(),
    )


def _handler(router, name, *, callback=False):
    observer = router.callback_query if callback else router.message
    return next(entry.callback for entry in observer.handlers
                if entry.callback.__name__ == name)


@asynccontextmanager
async def _scope(*_args, **_kwargs):
    yield MagicMock()


@pytest.mark.parametrize("rich", [False, True])
async def test_receipt_prompt_tracks_actual_telegram_message_on_both_transports(
    monkeypatch, rich,
):
    from telegram_membership_saas import payment_review_ui

    monkeypatch.setattr(payment_review_ui, "rich_enabled", lambda: rich)
    bot = _bot()
    ident = uuid4()
    message_id = await send_receipt_prompt(
        bot, buyer_id=123, kind="subscription", request_id=ident,
        cancel_data="receiptstop:token",
    )
    assert message_id == 901
    if rich:
        bot.send_rich_message.assert_awaited_once()
        bot.send_message.assert_not_awaited()
    else:
        bot.send_message.assert_awaited_once()
        bot.send_rich_message.assert_not_awaited()
    assert "reply_parameters" not in (
        bot.send_rich_message.await_args.kwargs if rich
        else bot.send_message.await_args.kwargs
    )


def test_receipt_intent_tracks_prompt_only_for_exact_active_invoice():
    bot, buyer, first, second = 442, 123, uuid4(), uuid4()
    receipt_intents.arm(
        bot, buyer, kind="file", request_id=first, prompt_message_id=701,
    )
    assert receipt_intents.prompt_message(
        bot, buyer, kind="file", request_id=first,
    ) == 701
    receipt_intents.arm(
        bot, buyer, kind="file", request_id=first,
    )
    assert receipt_intents.prompt_message(
        bot, buyer, kind="file", request_id=first,
    ) == 701
    receipt_intents.arm(
        bot, buyer, kind="file", request_id=second, prompt_message_id=702,
    )
    assert receipt_intents.prompt_message(
        bot, buyer, kind="file", request_id=first,
    ) is None
    assert receipt_intents.prompt_message(
        bot, buyer, kind="file", request_id=second,
    ) == 702
    assert receipt_intents.prompt_message(
        bot, buyer + 1, kind="file", request_id=second,
    ) is None
    assert receipt_intents.prompt_message(
        bot, buyer, kind="subscription", request_id=second,
    ) is None
    assert receipt_intents.consume(bot, buyer, kind="file") == second
    assert receipt_intents.prompt_message(
        bot, buyer, kind="file", request_id=second,
    ) is None


@pytest.mark.parametrize(("language", "expected"), [
    ("ru", "✅ Чек отправлен"),
    ("en", "✅ Receipt sent"),
])
async def test_sent_receipt_becomes_disabled_success_and_never_clickable(
    language, expected,
):
    bot = _bot()
    token = _language.set(language)
    try:
        changed = await update_receipt_prompt(
            bot, buyer_id=123, message_id=901, status="sent",
        )
    finally:
        _language.reset(token)
    assert changed
    kwargs = bot.edit_message_reply_markup.await_args.kwargs
    assert kwargs["chat_id"] == 123 and kwargs["message_id"] == 901
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == expected and button.style == "success"
    assert button.disabled is not None and button.callback_data is None


async def test_stopped_receipt_is_inactive_and_failed_review_remains_retryable():
    bot = _bot()
    await update_receipt_prompt(
        bot, buyer_id=123, message_id=901, status="stopped",
    )
    stopped = bot.edit_message_reply_markup.await_args.kwargs[
        "reply_markup"
    ].inline_keyboard[0][0]
    assert stopped.disabled is not None
    bot.edit_message_reply_markup.reset_mock()
    await update_receipt_prompt(
        bot, buyer_id=123, message_id=901,
        status="retry", retry_data="receiptretry:abc",
    )
    retried = bot.edit_message_reply_markup.await_args.kwargs[
        "reply_markup"
    ].inline_keyboard[0][0]
    assert retried.callback_data == "receiptretry:abc"
    assert retried.disabled is None


async def test_disabled_button_api_rejection_removes_old_action_instead():
    bot = _bot()
    bot.edit_message_reply_markup.side_effect = [
        TelegramBadRequest(
            method=EditMessageReplyMarkup(chat_id=123, message_id=901),
            message="Bad Request: unsupported disabled button",
        ),
        True,
    ]
    assert await update_receipt_prompt(
        bot, buyer_id=123, message_id=901, status="sent",
    )
    assert bot.edit_message_reply_markup.await_count == 2
    assert bot.edit_message_reply_markup.await_args.kwargs["reply_markup"] is None


async def test_unknown_prompt_message_has_no_edit_and_failure_is_safe():
    bot = _bot()
    assert not await update_receipt_prompt(
        bot, buyer_id=123, message_id=None, status="sent",
    )
    bot.edit_message_reply_markup.assert_not_awaited()
    bot.edit_message_reply_markup.side_effect = TelegramBadRequest(
        method=EditMessageReplyMarkup(chat_id=123, message_id=901),
        message="Bad Request: message can't be edited",
    )
    assert not await update_receipt_prompt(
        bot, buyer_id=123, message_id=901, status="sent",
    )


async def test_subscription_success_retires_only_selected_prompt(monkeypatch):
    project, request_id = uuid4(), uuid4()
    bot = _bot()
    receipt_intents.arm(
        bot.id, 123, kind="subscription",
        request_id=request_id, prompt_message_id=901,
    )
    submit = AsyncMock(return_value=SubmissionResult(
        request=SimpleNamespace(id=request_id),
        item=ReviewItem(
            request_id=request_id, customer_telegram_user_id=123,
            customer_username="buyer", plan_name="Premium",
            amount=Decimal("500"), currency_code="RUB",
            payment_method_name="Bank", status="waiting_review",
        ),
        reviewer_telegram_ids=(456,), newly_submitted=True,
    ))
    monkeypatch.setattr(subs, "system_project_session_scope", _scope)
    monkeypatch.setattr(subs, "recipient_language", _scope)
    monkeypatch.setattr(subs, "submit_checkout_for_telegram_user", submit)
    router = subs.create_telegram_commerce_router(MagicMock(), project_id=project)
    await _handler(router, "direct_receipt_handler")(_message(), bot)
    assert receipt_intents.peek(bot.id, 123, kind="subscription") is None
    opts = bot.edit_message_reply_markup.await_args.kwargs
    assert opts["message_id"] == 901
    btn = opts["reply_markup"].inline_keyboard[0][0]
    assert btn.style == "success" and btn.disabled is not None
    submit.assert_awaited_once()


async def test_subscription_failure_offers_retry_on_same_prompt(monkeypatch):
    project, request_id = uuid4(), uuid4()
    bot = _bot()
    receipt_intents.arm(
        bot.id, 123, kind="subscription",
        request_id=request_id, prompt_message_id=901,
    )
    item = ReviewItem(
        request_id=request_id, customer_telegram_user_id=123,
        customer_username="buyer", plan_name="Premium",
        amount=Decimal("500"), currency_code="RUB",
        payment_method_name="Bank", status="waiting_review",
    )
    monkeypatch.setattr(subs, "system_project_session_scope", _scope)
    monkeypatch.setattr(subs, "submit_checkout_for_telegram_user", AsyncMock(
        return_value=SubmissionResult(
            request=SimpleNamespace(id=request_id), item=item,
            reviewer_telegram_ids=(), newly_submitted=True,
        ),
    ))
    router = subs.create_telegram_commerce_router(MagicMock(), project_id=project)
    await _handler(router, "direct_receipt_handler")(_message(), bot)
    markup = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "🔁 Повторить чек"
    assert markup.inline_keyboard[0][0].callback_data.startswith("receiptretry:")
    assert bot.send_message.await_args.kwargs["reply_markup"] is None


async def test_file_success_retires_exact_prompt(monkeypatch):
    project, order_id = uuid4(), uuid4()
    bot = _bot()
    receipt_intents.arm(
        bot.id, 123, kind="file", request_id=order_id,
        prompt_message_id=901,
    )
    order = SimpleNamespace(
        id=order_id, status="submitted", telegram_user_id=123,
        product_name="File", amount=Decimal("100"), currency_code="RUB",
        payment_method_name="Bank", receipt_kind="photo",
        receipt_file_id="proof",
    )
    monkeypatch.setattr(
        file_manual, "submit_manual_receipt",
        AsyncMock(return_value=(order, (456,))),
    )
    monkeypatch.setattr(file_manual, "recipient_language", _scope)
    router = file_manual.create_manual_file_router(MagicMock(), project_id=project)
    await _handler(router, "direct_file_receipt")(_message(), bot)
    assert receipt_intents.peek(bot.id, 123, kind="file") is None
    assert bot.edit_message_reply_markup.await_args.kwargs["message_id"] == 901
    btn = bot.edit_message_reply_markup.await_args.kwargs[
        "reply_markup"
    ].inline_keyboard[0][0]
    assert btn.style == "success" and btn.disabled is not None


def _folder(name, status="active"):
    return SimpleNamespace(id=uuid4(), name=name, status=status)


def _product(name, status="draft", *, stars_price=None):
    return SimpleNamespace(
        id=uuid4(), name=name, status=status, stars_price=stars_price,
        description="Description <hello> & details",
        folder_id=uuid4(),
    )


@pytest.mark.parametrize(("lang", "folders_title", "products_title"), [
    ("ru", "Управление папками", "Товары папки"),
    ("en", "Manage folders", "Folder products"),
])
def test_file_admin_inventory_rich_escapes_and_localizes(
    lang, folders_title, products_title,
):
    folders = [_folder("<Owner> & Files"), _folder("Old", "archived")]
    products = [_product("<File> & Media", "published")]
    token = _language.set(lang)
    try:
        a = rich_ui.rich_admin_folders(folders)
        b = rich_ui.rich_admin_products(products)
    finally:
        _language.reset(token)
    assert folders_title in a and products_title in b
    assert "<table bordered compact>" in a and b
    assert "&lt;Owner&gt; &amp; Files" in a
    assert "&lt;File&gt; &amp; Media" in b
    assert "<Owner>" not in a
    assert "Всего" in a if lang == "ru" else "Total" in a


@pytest.mark.parametrize(("lang", "title"), [
    ("ru", "Управление товаром"),
    ("en", "Manage product"),
])
def test_file_admin_card_shows_stars_exact_currency_prices_and_description(
    lang, title,
):
    product = _product("<Product> & Sale", "published", stars_price=500)
    token = _language.set(lang)
    try:
        html = rich_ui.rich_admin_product_detail(
            product, file_count=3,
            prices=[("RUB", Decimal("100.00")), ("EUR", Decimal("2.50")),
                    ("UAH", None)],
        )
    finally:
        _language.reset(token)
    assert title in html
    assert "<table bordered compact>" in html
    assert "&lt;Product&gt; &amp; Sale" in html
    assert "<th>⭐ Telegram Stars</th><td>500</td>" in html
    assert "XTR" not in html
    assert "100" in html and "2.5" in html
    assert "100.00" not in html and "2.50" not in html
    assert "<hello>" not in html and "&lt;hello&gt;" in html
    assert "UAH" not in html
    assert "<details><summary>" in html


async def test_file_management_routes_keep_owner_only_actions_and_buyer_classic(
    monkeypatch,
):
    project, owner_actor = uuid4(), uuid4()
    folders = [_folder("Files")]
    product = _product("<File> & more", "draft", stars_price=100)
    product.folder_id = folders[0].id
    monkeypatch.setattr(
        file_admin, "ensure_telegram_actor",
        AsyncMock(return_value=owner_actor),
    )
    monkeypatch.setattr(
        file_admin, "can_manage_project", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        file_admin, "list_folders", AsyncMock(return_value=tuple(folders)),
    )
    monkeypatch.setattr(
        file_admin, "list_products", AsyncMock(return_value=(product,)),
    )
    monkeypatch.setattr(
        file_admin, "view_product", AsyncMock(return_value=(product, 2)),
    )
    monkeypatch.setattr(
        file_admin, "price_editor_options",
        AsyncMock(return_value=(("RUB", Decimal("100")),)),
    )
    rich_send = AsyncMock(return_value=True)
    monkeypatch.setattr(file_admin, "send_file_card", rich_send)
    router = file_admin.create_file_sales_router(MagicMock(), project_id=project)
    cb = _callback("fs:adminfolders")
    bot = _bot()
    await _handler(router, "file_callback", callback=True)(cb, bot)
    rich_send.assert_awaited_once()
    opts = rich_send.await_args.kwargs
    assert "Управление папками" in opts["rich_html"]
    assert any(
        button.callback_data.startswith("fs:af:")
        for row in opts["reply_markup"].inline_keyboard for button in row
    )
    rich_send.reset_mock()
    await _handler(router, "file_callback", callback=True)(
        _callback("fs:af:" + file_admin.encode_uuid(folders[0].id)), bot,
    )
    assert "Товары папки" in rich_send.await_args.kwargs["rich_html"]
    rich_send.reset_mock()
    await _handler(router, "file_callback", callback=True)(
        _callback("fs:ap:" + file_admin.encode_uuid(product.id)), bot,
    )
    opts = rich_send.await_args.kwargs
    assert "Управление товаром" in opts["rich_html"]
    assert "📎 Файлов" in opts["rich_html"]
    buttons = [
        button.callback_data
        for row in opts["reply_markup"].inline_keyboard for button in row
    ]
    assert "fs:editname:" + file_admin.encode_uuid(product.id) in buttons
    assert not any(cb.startswith("fs:richpreview:") for cb in buttons)
    assert "fs:editname:" + file_admin.encode_uuid(product.id) in buttons
    assert "fs:af:" + file_admin.encode_uuid(product.folder_id) in buttons

    monkeypatch.setattr(
        file_admin, "can_manage_project", AsyncMock(return_value=False),
    )
    rich_send.reset_mock()
    await _handler(router, "file_callback", callback=True)(
        _callback("fs:shop"), bot,
    )
    rich_send.assert_not_awaited()
    bot.send_message.assert_awaited()
