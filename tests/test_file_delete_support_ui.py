"""Delete controls require owner confirmation; buyer support remains one tap away."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.file_sales import handlers as ui
from telegram_membership_saas.modules.telegram_commerce.account import support_screen
from telegram_membership_saas.modules.telegram_commerce.handlers import (
    _account_navigation_keyboard,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    CheckoutOption,
    encode_uuid,
)
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    BuyerOffer,
    buyer_root_screen,
    catalog_screen,
)
from telegram_membership_saas.support_links import SUPPORT_URL


def _bot():
    bot = MagicMock()
    bot.id = 19001
    bot.send_message = AsyncMock()
    return bot


def _query(action):
    return SimpleNamespace(
        data="fs:" + action,
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
        answer=AsyncMock(),
    )


def _handler(router):
    return next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "file_callback"
    )


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _buyer_offer():
    option = CheckoutOption(
        plan_id=uuid4(), plan_name="1 month",
        duration_value=1, duration_unit="month",
        plan_price_id=uuid4(), currency_code="RUB",
        amount=Decimal("500"), currency_precision=2,
        payment_method_id=uuid4(), payment_method_name="Manual",
    )
    return BuyerOffer(
        option=option, methods=(option,), resource_label="Private channel",
    )


@pytest.mark.parametrize(("lang", "label"), [
    ("ru", "🆘 Поддержка"),
    ("en", "🆘 Support"),
])
def test_official_support_button_in_all_three_buyer_surfaces(lang, label):
    token = _language.set(lang)
    try:
        channel = uuid4()
        _text, root = buyer_root_screen("My channel", ((channel, "Files"),))
        _text, catalog = catalog_screen((_buyer_offer(),))
        _text, empty_catalog = catalog_screen(())
        sent = _account_navigation_keyboard()
        _help_text, help_markup = support_screen()
        for keyboard in (root, catalog, empty_catalog, sent, help_markup):
            contacts = [
                button for button in _buttons(keyboard)
                if button.url == SUPPORT_URL
            ]
            assert len(contacts) == 1
            assert contacts[0].text == label
            assert contacts[0].callback_data is None
        # Keep primary existing navigation, with support next to Language.
        assert root.inline_keyboard[-1][0].callback_data == "lng:menu"
        assert root.inline_keyboard[-1][1].url == SUPPORT_URL
        assert catalog.inline_keyboard[-1][0].callback_data == "lng:menu"
        assert catalog.inline_keyboard[-1][1].url == SUPPORT_URL
        assert sent.inline_keyboard[0][0].callback_data == "account:catalog"
        assert sent.inline_keyboard[0][1].url == SUPPORT_URL
        assert "not configured" not in _help_text
        assert "не настроен" not in _help_text
    finally:
        _language.reset(token)


async def test_folder_and_product_deletion_requires_explicit_confirmation(monkeypatch):
    folder_id, product_id = uuid4(), uuid4()
    project_id = uuid4()
    owner_id = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner_id))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "list_folders", AsyncMock(return_value=(
        SimpleNamespace(id=folder_id, name="Owner <folder>", status="active"),
    )))
    monkeypatch.setattr(ui, "list_products", AsyncMock(return_value=()))
    monkeypatch.setattr(ui, "view_product", AsyncMock(return_value=(
        SimpleNamespace(
            id=product_id, name="Premium <video>",
            status="draft", folder_id=folder_id,
        ), 0,
    )))
    folder_delete = AsyncMock()
    product_delete = AsyncMock(return_value=folder_id)
    monkeypatch.setattr(ui, "delete_empty_folder", folder_delete)
    monkeypatch.setattr(ui, "delete_unsold_product", product_delete)
    monkeypatch.setattr(ui, "send_file_card", AsyncMock())
    router = ui.create_file_sales_router(MagicMock(), project_id=project_id)
    bot = _bot()
    handler = _handler(router)
    folder_code, product_code = encode_uuid(folder_id), encode_uuid(product_id)

    await handler(_query("delfolderask:" + folder_code), bot)
    folder_delete.assert_not_awaited()
    call = bot.send_message.await_args
    assert "&lt;folder&gt;" in call.args[1]
    assert "fs:delfolderok:" + folder_code in [
        x.callback_data for x in _buttons(call.kwargs["reply_markup"])
    ]
    await handler(_query("delfolderok:" + folder_code), bot)
    folder_delete.assert_awaited_once()
    assert folder_delete.await_args.kwargs["actor_id"] == owner_id
    assert folder_delete.await_args.kwargs["folder_id"] == folder_id

    bot.send_message.reset_mock()
    await handler(_query("delproductask:" + product_code), bot)
    product_delete.assert_not_awaited()
    call = bot.send_message.await_args
    assert "&lt;video&gt;" in call.args[1]
    assert "fs:delproductok:" + product_code in [
        x.callback_data for x in _buttons(call.kwargs["reply_markup"])
    ]
    await handler(_query("delproductok:" + product_code), bot)
    product_delete.assert_awaited_once()
    assert product_delete.await_args.kwargs["product_id"] == product_id
    assert product_delete.await_args.kwargs["project_id"] == project_id


async def test_individual_file_removal_never_exposes_file_id(monkeypatch):
    project_id, product_id, asset_id, actor = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    bot = _bot()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    product = SimpleNamespace(
        id=product_id, status="draft", name="Test <file>",
    )
    asset = SimpleNamespace(id=asset_id, kind="video", sort_order=0,
                            file_id="PRIVATE_TELEGRAM_FILE_ID")
    monkeypatch.setattr(ui, "list_product_assets", AsyncMock(
        return_value=(product, (asset,)),
    ))
    card = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "send_file_card", card)
    deleted = AsyncMock(return_value=0)
    monkeypatch.setattr(ui, "delete_draft_asset", deleted)
    router = ui.create_file_sales_router(MagicMock(), project_id=project_id)
    handler = _handler(router)
    p, a = encode_uuid(product_id), encode_uuid(asset_id)
    await handler(_query("assets:" + p), bot)
    card.assert_awaited_once()
    kwargs = card.await_args.kwargs
    assert "&lt;file&gt;" in kwargs["rich_html"]
    assert "PRIVATE_TELEGRAM_FILE_ID" not in str(kwargs)
    callbacks = [x.callback_data for x in _buttons(kwargs["reply_markup"])]
    assert "fs:delassetask:" + p + ":" + a in callbacks
    assert all(len(s.encode()) <= 64 for s in callbacks)
    assert deleted.await_count == 0

    await handler(_query("delassetask:" + p + ":" + a), bot)
    deleted.assert_not_awaited()
    call = bot.send_message.await_args
    assert "fs:delassetok:" + p + ":" + a in [
        x.callback_data for x in _buttons(call.kwargs["reply_markup"])
    ]
    await handler(_query("delassetok:" + p + ":" + a), bot)
    deleted.assert_awaited_once()
    assert deleted.await_args.kwargs["asset_id"] == asset_id
    assert deleted.await_args.kwargs["product_id"] == product_id


@pytest.mark.parametrize("action", ["delfolderok:", "delproductok:", "delassetok:"])
async def test_forged_deletion_callback_never_reaches_services(monkeypatch, action):
    project, ident, actor = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    folder = AsyncMock()
    product = AsyncMock()
    asset = AsyncMock()
    monkeypatch.setattr(ui, "delete_empty_folder", folder)
    monkeypatch.setattr(ui, "delete_unsold_product", product)
    monkeypatch.setattr(ui, "delete_draft_asset", asset)
    token = encode_uuid(ident)
    payload = action + token + (":" + token if action == "delassetok:" else "")
    q = _query(payload)
    await _handler(ui.create_file_sales_router(MagicMock(), project_id=project))(q, _bot())
    folder.assert_not_awaited()
    product.assert_not_awaited()
    asset.assert_not_awaited()
    assert q.answer.await_args.kwargs["show_alert"] is True


async def test_product_and_folder_maintenance_controls_are_present(monkeypatch):
    project, folder, product, actor = uuid4(), uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "list_products", AsyncMock(return_value=()))
    monkeypatch.setattr(ui, "view_product", AsyncMock(return_value=(
        SimpleNamespace(
            id=product, folder_id=folder, name="Video",
            status="draft", stars_price=None, description="",
        ), 2,
    )))
    monkeypatch.setattr(ui, "price_editor_options", AsyncMock(return_value=()))
    sent = AsyncMock()
    monkeypatch.setattr(ui, "send_file_card", sent)
    bot = _bot()
    handler = _handler(ui.create_file_sales_router(MagicMock(), project_id=project))
    await handler(_query("af:" + encode_uuid(folder)), bot)
    buttons = [x.callback_data for x in _buttons(
        sent.await_args.kwargs["reply_markup"]
    )]
    assert "fs:delfolderask:" + encode_uuid(folder) in buttons
    sent.reset_mock()
    await handler(_query("ap:" + encode_uuid(product)), bot)
    buttons = [x.callback_data for x in _buttons(
        sent.await_args.kwargs["reply_markup"]
    )]
    assert "fs:delproductask:" + encode_uuid(product) in buttons
    assert "fs:assets:" + encode_uuid(product) in buttons
    assert "fs:richpreview:" + encode_uuid(product) not in buttons
