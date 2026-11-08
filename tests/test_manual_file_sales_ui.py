"""File buyer UI: manual methods are shown ahead of optional Stars checkout."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.file_sales import manual_handlers as ui
from telegram_membership_saas.modules.file_sales.manual_service import ManualMethod
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


@pytest.fixture(autouse=True)
def _default_stars_visibility(monkeypatch):
    # Buyer checkout tests isolate payment UI from a live database.
    monkeypatch.setattr(ui, "file_stars_enabled", AsyncMock(return_value=True))


def _bot():
    bot = MagicMock()
    bot.id = 9999
    bot.send_message = AsyncMock()
    return bot


def _buttons(markup):
    return [item for row in markup.inline_keyboard for item in row]


def _query(action, user_id=123):
    return SimpleNamespace(
        data="fm:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=user_id)),
        from_user=SimpleNamespace(
            id=user_id, username="buyer", first_name="Buyer",
        ),
        answer=AsyncMock(),
    )


def _handler(router):
    return next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "file_manual_callback"
    )


async def test_buyer_file_picker_shows_eligible_manual_currencies_and_methods(
    monkeypatch,
):
    product_id, folder = uuid4(), uuid4()
    factory, project = MagicMock(), uuid4()
    offers = (
        ManualMethod("RUB", Decimal("525"), 2, uuid4(), "СберБанк"),
        ManualMethod("RUB", Decimal("525"), 2, uuid4(), "Т-БАНК"),
        ManualMethod("USD", Decimal("8"), 2, uuid4(), "Boosty"),
    )
    monkeypatch.setattr(ui, "available_methods", AsyncMock(return_value=offers))
    monkeypatch.setattr(ui, "view_product", AsyncMock(
        return_value=(SimpleNamespace(
            id=product_id, name="ВИДЕО 720p", stars_price=300,
            folder_id=folder,
        ), 1),
    ))
    bot = _bot()
    await ui.show_file_payment_options(
        bot, factory=factory, project_id=project,
        buyer_id=123, product_id=product_id,
    )
    call = bot.send_message.await_args
    assert "ВИДЕО 720p" in call.args[1]
    assert "525 RUB" in call.args[1]
    keys = _buttons(call.kwargs["reply_markup"])
    callbacks = [button.callback_data for button in keys]
    assert callbacks[:2] == [
        "fm:currency:" + encode_uuid(product_id) + ":RUB",
        "fm:currency:" + encode_uuid(product_id) + ":USD",
    ]
    assert "fm:method:" + encode_uuid(product_id) + ":RUB:" + encode_uuid(
        offers[0].method_id,
    ) in callbacks
    assert "fm:method:" + encode_uuid(product_id) + ":USD:" + encode_uuid(
        offers[2].method_id,
    ) not in callbacks
    assert "fs:buy:" + encode_uuid(product_id) in callbacks


async def test_manual_only_product_has_no_stars_button(monkeypatch):
    product = uuid4()
    monkeypatch.setattr(ui, "available_methods", AsyncMock(return_value=(
        ManualMethod("RUB", Decimal("525"), 2, uuid4(), "ВТБ"),
    )))
    monkeypatch.setattr(ui, "view_product", AsyncMock(
        return_value=(SimpleNamespace(
            name="Видео", stars_price=None, folder_id=uuid4(),
        ), 1),
    ))
    bot = _bot()
    await ui.show_file_payment_options(
        bot, factory=MagicMock(), project_id=uuid4(),
        buyer_id=123, product_id=product,
    )
    assert not any(
        button.callback_data.startswith("fs:buy:")
        for button in _buttons(bot.send_message.await_args.kwargs["reply_markup"])
    )


async def test_method_checkout_shows_bank_requisites_and_proof_button_without_delivery(
    monkeypatch,
):
    project, product, method, order_id = uuid4(), uuid4(), uuid4(), uuid4()
    router = ui.create_manual_file_router(MagicMock(), project_id=project)
    created = SimpleNamespace(
        id=order_id, status="pending", product_name="Видео",
        currency_code="RUB", payment_method_name="СберБанк",
        amount=Decimal("525"), instruction="Перевести на карту 1111",
    )
    create = AsyncMock(return_value=(created, False))
    monkeypatch.setattr(ui, "create_manual_order", create)
    monkeypatch.setattr(ui, "purchased_assets", AsyncMock())
    bot = _bot()
    callback = _query(
        "method:" + encode_uuid(product) + ":RUB:" + encode_uuid(method),
    )
    await _handler(router)(callback, bot)
    create.assert_awaited_once()
    assert create.await_args.kwargs["product_id"] == product
    assert create.await_args.kwargs["method_id"] == method
    assert create.await_args.kwargs["buyer_id"] == 123
    assert "Перевести на карту 1111" in bot.send_message.await_args.args[1]
    keys = _buttons(bot.send_message.await_args.kwargs["reply_markup"])
    assert any(
        b.callback_data == "fm:paid:" + encode_uuid(order_id) for b in keys
    )
    ui.purchased_assets.assert_not_awaited()


async def test_untrusted_file_receipt_prompt_is_ignored():
    order = uuid4()
    authentic = SimpleNamespace(
        text=ui.RECEIPT_PREFIX + str(order) + "\nReply with receipt",
        from_user=SimpleNamespace(id=9999),
    )
    message = SimpleNamespace(
        text=None, chat=SimpleNamespace(type="private", id=123),
        reply_to_message=authentic,
    )
    assert ui._receipt_order_id(message, 9999) == order
    assert await ui.ManualReceiptFilter()(message, _bot())
    authentic.from_user.id = 7777
    assert ui._receipt_order_id(message, 9999) is None
    assert not await ui.ManualReceiptFilter()(message, _bot())


async def test_file_picker_preserves_escaped_description(monkeypatch):
    product = uuid4()
    monkeypatch.setattr(ui, "available_methods", AsyncMock(return_value=(
        ManualMethod("RUB", Decimal("525"), 2, uuid4(), "Bank"),
    )))
    monkeypatch.setattr(ui, "view_product", AsyncMock(return_value=(
        SimpleNamespace(
            name="Video", description="Full HD <1080p> & extras",
            stars_price=None, folder_id=uuid4(),
        ), 1,
    )))
    bot = _bot()
    await ui.show_file_payment_options(
        bot, factory=MagicMock(), project_id=uuid4(),
        buyer_id=123, product_id=product,
    )
    body = bot.send_message.await_args.args[1]
    assert "Full HD &lt;1080p&gt; &amp; extras" in body
    assert "525 RUB" in body


async def test_payment_manager_can_open_review_queue_from_own_command(monkeypatch):
    from telegram_membership_saas.telegram_menus import commands_for

    assert [c.command for c in commands_for("payment_manager", "ru")] == [
        "menu", "filepayments", "support",
    ]
    project = uuid4()
    router = ui.create_manual_file_router(MagicMock(), project_id=project)
    handler = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "file_payment_reviews_command"
    )
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    pending = AsyncMock(return_value=(37, ()))
    monkeypatch.setattr(ui, "pending_manual_reviews", pending)
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=123, username="manager", first_name="Manager",
        ),
        chat=SimpleNamespace(type="private", id=123),
    )
    bot = _bot()
    await handler(message, bot)
    assert "37" in bot.send_message.await_args.args[1]
    assert pending.await_args.kwargs["project_id"] == project
    assert any(
        b.callback_data == "fm:reviews"
        for b in _buttons(bot.send_message.await_args.kwargs["reply_markup"])
    )


async def test_auto_stars_switch_does_not_hide_manual_method_named_stars(monkeypatch):
    product = uuid4()
    method = ManualMethod(
        "RUB", Decimal("100"), 2, uuid4(), "Telegram Stars ⭐",
    )
    monkeypatch.setattr(ui, "available_methods", AsyncMock(return_value=(method,)))
    monkeypatch.setattr(ui, "file_stars_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "view_product", AsyncMock(return_value=(
        SimpleNamespace(
            id=product, name="Видео", stars_price=500,
            folder_id=uuid4(), description="",
        ), 1,
    )))
    bot = _bot()
    await ui.show_file_payment_options(
        bot, factory=MagicMock(), project_id=uuid4(),
        buyer_id=123, product_id=product,
    )
    buttons = _buttons(bot.send_message.await_args.kwargs["reply_markup"])
    assert any(b.text == "💳 Telegram Stars ⭐" for b in buttons)
    assert not any(b.callback_data == "fs:buy:" + encode_uuid(product) for b in buttons)


async def test_all_file_payment_methods_disabled_shows_back_navigation(monkeypatch):
    product, folder = uuid4(), uuid4()
    monkeypatch.setattr(ui, "available_methods", AsyncMock(return_value=()))
    monkeypatch.setattr(ui, "file_stars_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "view_product", AsyncMock(return_value=(
        SimpleNamespace(
            id=product, name="Видео", stars_price=500,
            folder_id=folder, description="",
        ), 1,
    )))
    bot = _bot()
    await ui.show_file_payment_options(
        bot, factory=MagicMock(), project_id=uuid4(),
        buyer_id=123, product_id=product,
    )
    call = bot.send_message.await_args
    assert "временно отключены" in call.args[1]
    assert [b.callback_data for b in _buttons(call.kwargs["reply_markup"])] == [
        "fs:folder:" + encode_uuid(folder),
    ]
