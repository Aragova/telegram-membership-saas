"""File-sales screens and errors must honor each user's selected RU/EN locale."""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas import localization
from telegram_membership_saas.modules.file_sales import handlers as files_ui
from telegram_membership_saas.modules.file_sales import manual_handlers as manual_ui
from telegram_membership_saas.modules.file_sales import service as files_service
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _file_query(product):
    return SimpleNamespace(
        data="fs:ap:" + encode_uuid(product),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        answer=AsyncMock(),
    )


def _router_callback(router, name):
    return next(h.callback for h in router.callback_query.handlers
                if h.callback.__name__ == name)


@pytest.mark.parametrize(
    ("lang", "raw", "expected", "other"),
    [
        ("ru", "draft", "Статус: Черновик", "Status:"),
        ("ru", "published", "Статус: В продаже", "Status:"),
        ("ru", "archived", "Статус: Снят с продажи", "Status:"),
        ("en", "draft", "Status: Draft", "Статус:"),
        ("en", "published", "Status: Published", "Статус:"),
        ("en", "archived", "Status: Archived", "Статус:"),
    ],
)
async def test_admin_product_card_localizes_stored_status(
    monkeypatch, lang, raw, expected, other,
):
    product = uuid4()
    bot = MagicMock()
    bot.id = 8000
    bot.send_message = AsyncMock()
    monkeypatch.setattr(files_ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(files_ui, "can_manage_project",
                        AsyncMock(return_value=True))
    monkeypatch.setattr(files_ui, "view_product", AsyncMock(return_value=(
        SimpleNamespace(
            id=product, folder_id=uuid4(), name="Original product",
            description="", stars_price=500, status=raw,
        ), 1,
    )))
    monkeypatch.setattr(files_ui, "price_editor_options", AsyncMock(
        return_value=(("RUB", Decimal("100")),),
    ))
    callback = _router_callback(
        files_ui.create_file_sales_router(MagicMock(), project_id=uuid4()),
        "file_callback",
    )
    token = localization._language.set(lang)
    try:
        await callback(_file_query(product), bot)
    finally:
        localization._language.reset(token)
    text = bot.send_message.await_args.args[1]
    assert expected in text
    assert other not in text
    # Raw DB statuses are not presented to users, regardless of language.
    assert "Статус: " + raw not in text
    assert "Status: " + raw not in text


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("ru", "Название папки: от 1 до 100 символов"),
        ("en", "Folder name must contain 1–100 characters"),
    ],
)
async def test_validation_error_translated_without_database(lang, expected):
    token = localization._language.set(lang)
    try:
        with pytest.raises(ValueError, match=expected):
            await files_service.add_folder(
                MagicMock(), project_id=uuid4(), bot_id=123,
                actor_id=uuid4(), name="",
            )
    finally:
        localization._language.reset(token)


async def test_file_receipt_admin_notification_uses_reviewer_language(monkeypatch):
    """A Russian buyer's receipt must not force Russian UI on an English admin."""
    order_id = uuid4()
    message = SimpleNamespace(
        chat=SimpleNamespace(id=123, type="private"),
        from_user=SimpleNamespace(id=123),
        text=None,
        photo=[SimpleNamespace(file_id="receipt")],
        document=None,
        reply_to_message=SimpleNamespace(
            text=manual_ui.RECEIPT_PREFIX + str(order_id),
            from_user=SimpleNamespace(id=8000),
        ),
    )
    bot = MagicMock()
    bot.id = 8000
    bot.send_photo = AsyncMock()
    bot.send_document = AsyncMock()
    bot.send_message = AsyncMock()
    monkeypatch.setattr(manual_ui, "submit_manual_receipt", AsyncMock(
        return_value=(SimpleNamespace(
            id=order_id, product_name="Product", amount=Decimal("100"),
            currency_code="RUB", receipt_kind="photo", receipt_file_id="receipt",
        ), (456,)),
    ))
    @asynccontextmanager
    async def reviewer_english(factory, reviewer):
        assert reviewer == 456
        token = localization._language.set("en")
        try:
            yield
        finally:
            localization._language.reset(token)

    monkeypatch.setattr(manual_ui, "recipient_language", reviewer_english)
    router = manual_ui.create_manual_file_router(MagicMock(), project_id=uuid4())
    handler = next(x.callback for x in router.message.handlers
                   if x.callback.__name__ == "manual_receipt")
    token = localization._language.set("ru")
    try:
        await handler(message, bot)
    finally:
        localization._language.reset(token)
    admin = next(
        call for call in bot.send_message.await_args_list
        if (call.args[0] if call.args else call.kwargs.get("chat_id")) == 456
    )
    buttons = [b.text for row in admin.kwargs["reply_markup"].inline_keyboard
               for b in row]
    admin_text = admin.args[1] if len(admin.args) > 1 else admin.kwargs["text"]
    assert "Verify receipt of funds." in admin_text
    assert buttons == ["✅ Approve", "❌ Reject"]
    buyer = next(
        call for call in bot.send_message.await_args_list
        if (call.args[0] if call.args else call.kwargs.get("chat_id")) == 123
    )
    assert "Чек зарегистрирован" in buyer.args[1]


@pytest.mark.parametrize(
    ("lang", "header", "prompt", "stars_label"),
    [
        ("ru", "Оформление покупки файла", "Как вам удобнее оплатить?",
         "Звёзды Telegram"),
        ("en", "File purchase checkout", "How would you like to pay?",
         "Telegram Stars"),
    ],
)
async def test_buyer_file_payment_screen_is_ru_or_en(
    monkeypatch, lang, header, prompt, stars_label,
):
    from telegram_membership_saas.modules.file_sales.manual_service import (
        ManualMethod,
    )

    product = uuid4()
    monkeypatch.setattr(manual_ui, "available_methods", AsyncMock(return_value=(
        ManualMethod("RUB", Decimal("100"), 2, uuid4(), "Bank"),
    )))
    monkeypatch.setattr(
        manual_ui, "file_stars_enabled", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(manual_ui, "view_product", AsyncMock(return_value=(
        SimpleNamespace(
            id=product, folder_id=uuid4(),
            name="Original product", description="", stars_price=500,
        ), 1,
    )))
    bot = MagicMock()
    bot.id = 8000
    bot.send_message = AsyncMock()
    token = localization._language.set(lang)
    try:
        await manual_ui.show_file_payment_options(
            bot, factory=MagicMock(), project_id=uuid4(),
            buyer_id=123, product_id=product,
        )
    finally:
        localization._language.reset(token)
    call = bot.send_message.await_args
    assert header in call.args[1]
    assert prompt in call.args[1]
    labels = [b.text for row in call.kwargs["reply_markup"].inline_keyboard
              for b in row]
    assert any(stars_label in text for text in labels)


@pytest.mark.parametrize(
    ("lang", "heading", "bulk_label", "stars_label"),
    [
        ("ru", "Способы оплаты файлов", "Выключить все способы оплаты",
         "Автоматическая оплата звёздами"),
        ("en", "File payment methods", "Disable all payment methods",
         "Automatic Stars"),
    ],
)
async def test_owner_file_payment_methods_localize_both_languages(
    monkeypatch, lang, heading, bulk_label, stars_label,
):
    from telegram_membership_saas.modules.file_sales.payment_controls import (
        AUTO_STARS_ID,
        FilePaymentChoice,
    )

    monkeypatch.setattr(files_ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(files_ui, "can_manage_project",
                        AsyncMock(return_value=True))
    monkeypatch.setattr(files_ui, "list_file_payment_controls", AsyncMock(
        return_value=(FilePaymentChoice(
            method_id=AUTO_STARS_ID, name="Stars XTR",
            enabled=True, auto_stars=True,
        ),),
    ))
    bot = MagicMock()
    bot.id = 8000
    bot.send_message = AsyncMock()
    router = files_ui.create_file_sales_router(
        MagicMock(), project_id=uuid4(),
    )
    callback = _router_callback(router, "file_callback")
    query = SimpleNamespace(
        data="fs:paymethods",
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        answer=AsyncMock(),
    )
    token = localization._language.set(lang)
    try:
        await callback(query, bot)
    finally:
        localization._language.reset(token)
    call = bot.send_message.await_args
    assert heading in call.args[1]
    labels = [b.text for row in call.kwargs["reply_markup"].inline_keyboard
              for b in row]
    assert any(bulk_label in x for x in labels)
    assert any(stars_label in x for x in labels)
