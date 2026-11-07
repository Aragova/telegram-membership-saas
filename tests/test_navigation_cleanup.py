"""Regression: lean connected owner and buyer Telegram navigation (RU/EN).

No financial or subscription writes are needed for these UI tests.
"""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.telegram_commerce import handlers as buyer_ui
from telegram_membership_saas.modules.telegram_commerce.service import CheckoutOption, encode_uuid
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    build_buyer_offers,
    catalog_screen,
    offer_screen,
)
from telegram_membership_saas.telegram_menus import commands_for


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


@pytest.mark.parametrize(
    ("language", "settings_label", "back_label"),
    [("ru", "⚙️ Настройки", "⬅️ Панель администратора"),
     ("en", "⚙️ Settings", "⬅️ Admin dashboard")],
)
async def test_owner_dashboard_hides_settings_children_and_settings_has_five_sections(
    monkeypatch, language, settings_label, back_label,
):
    project_id, actor_id = uuid4(), uuid4()
    monkeypatch.setattr(
        owner_ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    monkeypatch.setattr(
        owner_ui, "can_manage_project", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        owner_ui, "get_selected_project", AsyncMock(return_value=project_id),
    )
    monkeypatch.setattr(
        owner_ui, "setup_overview",
        AsyncMock(return_value=SetupOverview(
            project_id=project_id, project_name="VIP",
            timezone="Europe/Moscow",
            bots=(("vip_bot", "registered"),),
            resources=(), plans=(), methods=(),
        )),
    )
    bot = MagicMock()
    bot.id = 123456
    bot.send_message = AsyncMock()
    router = owner_ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project_id,
    )
    handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )

    def query(action):
        return SimpleNamespace(
            data="own:" + action,
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            from_user=SimpleNamespace(id=9876, username="owner", first_name="Owner"),
            answer=AsyncMock(),
        )

    token = _language.set(language)
    try:
        await handler(query("dashboard"), bot)
        main_buttons = _buttons(bot.send_message.await_args.kwargs["reply_markup"])
        actions = [b.callback_data for b in main_buttons]
        assert "own:settings" in actions
        assert "own:notifications" not in actions
        assert "own:readiness" not in actions
        assert "own:timezone" not in actions
        assert "own:network" not in actions
        assert "own:help" not in actions
        assert "own:preview" in actions
        assert next(b.text for b in main_buttons if b.callback_data == "own:settings") == (
            settings_label
        )
        assert next(b.style for b in main_buttons if b.callback_data == "lng:menu") == (
            "success"
        )

        await handler(query("settings"), bot)
        settings_buttons = _buttons(bot.send_message.await_args.kwargs["reply_markup"])
        settings_actions = {b.callback_data for b in settings_buttons}
        assert settings_actions == {
            "own:notifications", "own:readiness", "own:timezone",
            "own:network", "own:modmanage", "own:transfer",
            "own:help", "own:dashboard",
        }
        assert next(
            b.text for b in settings_buttons if b.callback_data == "own:dashboard"
        ) == back_label
        assert len(bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard) == 7
    finally:
        _language.reset(token)


@pytest.mark.parametrize("language", ("ru", "en"))
def test_buyer_screens_do_not_duplicate_native_menu_purchases(language):
    plan_id = uuid4()
    option = CheckoutOption(
        plan_id=plan_id, plan_name="VIP", duration_value=1,
        duration_unit="month", plan_price_id=uuid4(),
        currency_code="RUB", amount=Decimal("550"), currency_precision=2,
        payment_method_id=uuid4(), payment_method_name="Bank",
    )
    offers = build_buyer_offers((option,), {plan_id: ("VIP channel",)})
    token = _language.set(language)
    try:
        _text, catalog_markup = catalog_screen(offers)
        catalog_buttons = _buttons(catalog_markup)
        catalog_actions = {b.callback_data for b in catalog_buttons}
        assert any(a.startswith("offer:") for a in catalog_actions)
        assert "lng:menu" in catalog_actions
        assert "account:home" not in catalog_actions
        assert next(b.style for b in catalog_buttons if b.callback_data == "lng:menu") == (
            "success"
        )
        _text, empty_markup = catalog_screen(())
        assert [b.callback_data for b in _buttons(empty_markup)] == ["lng:menu"]

        detail = offer_screen(offers, price_id=option.plan_price_id)
        assert detail is not None
        _text, offer_markup = detail
        offer_actions = {b.callback_data for b in _buttons(offer_markup)}
        assert any(a.startswith("buy:") for a in offer_actions)
        assert "cat:RUB:0" in offer_actions
        assert "lng:menu" not in offer_actions
        assert "account:home" not in offer_actions

        checkout = buyer_ui._checkout_keyboard(uuid4())
        checkout_actions = [b.callback_data for b in _buttons(checkout)]
        assert len(checkout_actions) == 3
        assert any(a.startswith("paid:") for a in checkout_actions)
        assert any(a.startswith("cancel:") for a in checkout_actions)
        assert "checkoutnav:catalog" in checkout_actions
        assert "checkoutnav:home" not in checkout_actions
        assert [b.callback_data for b in _buttons(
            buyer_ui._account_navigation_keyboard(),
        )] == ["account:catalog"]

        assert [c.command for c in commands_for("buyer", language)] == [
            "menu", "subscription", "support",
        ]
    finally:
        _language.reset(token)


@pytest.mark.parametrize("language", ("ru", "en"))
async def test_cancel_invoice_returns_to_catalog_not_purchase_history(
    monkeypatch, language,
):
    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield object()

    cancel = AsyncMock()
    catalog = AsyncMock(return_value=True)
    monkeypatch.setattr(buyer_ui, "system_project_session_scope", scope)
    monkeypatch.setattr(buyer_ui, "cancel_checkout_for_telegram_user", cancel)
    monkeypatch.setattr(buyer_ui, "_send_catalog", catalog)
    project = uuid4()
    request = uuid4()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    router = buyer_ui.create_telegram_commerce_router(MagicMock(), project_id=project)
    handle = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "cancel_handler"
    )
    callback = SimpleNamespace(
        data="cancel:" + encode_uuid(request),
        from_user=SimpleNamespace(id=123456),
        answer=AsyncMock(),
    )
    token = _language.set(language)
    try:
        await handle(callback, bot)
        cancel.assert_awaited_once()
        assert cancel.await_args.kwargs["request_id"] == request
        assert cancel.await_args.kwargs["project_id"] == project
        catalog.assert_awaited_once()
        assert catalog.await_args.kwargs["project_id"] == project
        assert catalog.await_args.kwargs["chat_id"] == 123456
        notice = catalog.await_args.kwargs["notice"]
        assert ("Заявка на оплату отменена" if language == "ru"
                else "Your payment request was canceled") in notice
        bot.send_message.assert_not_awaited()  # render once, with actual catalog
    finally:
        _language.reset(token)
