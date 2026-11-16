"""Free file storefront uses no payment provider or paid-purchase ledger."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.file_sales import handlers as files_ui
from telegram_membership_saas.modules.file_sales import manual_handlers as checkout_ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


async def test_free_buyer_card_has_only_download_and_back(monkeypatch):
    product_id, folder_id, project = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(checkout_ui, "view_product", AsyncMock(
        return_value=(SimpleNamespace(
            id=product_id, name="Video", description="Preview",
            folder_id=folder_id, stars_price=400, is_free=True,
        ), 1),
    ))
    methods = AsyncMock()
    monkeypatch.setattr(checkout_ui, "available_methods", methods)
    monkeypatch.setattr(checkout_ui, "file_stars_enabled", AsyncMock())
    send = AsyncMock()
    monkeypatch.setattr(checkout_ui, "send_file_card", send)
    bot = MagicMock(id=9876)
    await checkout_ui.show_file_payment_options(
        bot, factory=MagicMock(), project_id=project,
        buyer_id=123, product_id=product_id,
    )
    buttons = [
        button.callback_data for row in send.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert buttons == [
        "fs:free:" + encode_uuid(product_id),
        "fs:folder:" + encode_uuid(folder_id),
    ]
    assert "Бесплатно" in send.await_args.kwargs["legacy_html"] or (
        "Free" in send.await_args.kwargs["legacy_html"]
    )
    assert "Free file" in send.await_args.kwargs["rich_html"] or (
        "Бесплатный файл" in send.await_args.kwargs["rich_html"]
    )
    methods.assert_not_awaited()


async def test_free_callback_sends_media_without_invoice(monkeypatch):
    project, product = uuid4(), uuid4()
    router = files_ui.create_file_sales_router(MagicMock(), project_id=project)
    callback = next(
        handler.callback for handler in router.callback_query.handlers
        if handler.callback.__name__ == "file_callback"
    )
    monkeypatch.setattr(
        files_ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(files_ui, "can_manage_project", AsyncMock(return_value=False))
    source = AsyncMock(return_value=(
        SimpleNamespace(id=product, name="Free demo"),
        (SimpleNamespace(kind="document", file_id="original-bot-file"),),
    ))
    monkeypatch.setattr(files_ui, "free_product_assets", source)
    charged = AsyncMock()
    monkeypatch.setattr(files_ui, "create_stars_order", charged)
    bot = MagicMock()
    bot.id = 9876
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    bot.send_invoice = AsyncMock()
    q = SimpleNamespace(
        data="fs:free:" + encode_uuid(product),
        from_user=SimpleNamespace(id=123, username="buyer", first_name="Buyer"),
        message=SimpleNamespace(chat=SimpleNamespace(id=123, type="private")),
        answer=AsyncMock(),
    )
    await callback(q, bot)
    source.assert_awaited_once()
    assert source.await_args.kwargs["buyer_id"] == 123
    assert source.await_args.kwargs["bot_id"] == 9876
    bot.send_document.assert_awaited_once_with(123, "original-bot-file")
    bot.send_invoice.assert_not_awaited()
    charged.assert_not_awaited()
    q.answer.assert_awaited_once()
