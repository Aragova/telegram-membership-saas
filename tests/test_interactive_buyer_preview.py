"""Owner's read-only preview follows actual buyer offers without creating sales."""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.owner_setup.buyer_preview import (
    preview_catalog_screen,
    preview_method_screen,
    preview_offer_screen,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    CheckoutOption,
    encode_uuid,
)
from telegram_membership_saas.modules.telegram_commerce.storefront import BuyerOffer


def _offers():
    project_id = uuid4()
    month = uuid4()
    lifetime = uuid4()
    rub_price = uuid4()
    usd_price = uuid4()
    lifetime_price = uuid4()
    rub_method = uuid4()
    usd_method = uuid4()

    def option(plan_id, plan_price_id, method_id, currency, amount, duration):
        return CheckoutOption(
            plan_id=plan_id, plan_name="1 месяц" if duration else "Навсегда",
            duration_value=duration, duration_unit="month" if duration else "lifetime",
            plan_price_id=plan_price_id,
            currency_code=currency, amount=Decimal(amount),
            currency_precision=2, payment_method_id=method_id,
            payment_method_name="Bank & Co",
        )

    offers = (
        BuyerOffer(option(month, rub_price, rub_method, "RUB", "500", 1),
                   methods=(option(month, rub_price, rub_method, "RUB", "500", 1),),
                   resource_label="Channel <VIP>"),
        BuyerOffer(option(month, usd_price, usd_method, "USD", "8.50", 1),
                   methods=(option(month, usd_price, usd_method, "USD", "8.50", 1),),
                   resource_label="Channel <VIP>"),
        BuyerOffer(option(lifetime, lifetime_price, rub_method, "RUB", "999", None),
                   methods=(option(lifetime, lifetime_price, rub_method, "RUB", "999", None),),
                   resource_label="Channel <VIP>"),
    )
    return SimpleNamespace(
        project_id=project_id, month=month, lifetime=lifetime, offers=offers,
        rub_price=rub_price, usd_price=usd_price,
        rub_method=rub_method, usd_method=usd_method,
    )


def _buttons(keyboard):
    return [
        (button.text, button.callback_data)
        for row in keyboard.inline_keyboard for button in row
    ]


def test_preview_uses_actual_checkout_eligible_prices_and_duration_in_english():
    fixture = _offers()
    token = _language.set("en")
    try:
        content, keyboard = preview_catalog_screen(fixture.offers)
        assert "Buyer preview" in content
        assert "Channel &lt;VIP&gt;" in content
        assert "1 месяц · 1 month" in content
        assert "Навсегда · Lifetime" in content
        assert "500 RUB" in content
        assert "999 RUB" in content
        assert "No payment request" in content
        buttons = _buttons(keyboard)
        assert ("USD", "own:pvcat:USD:0") in buttons
        assert any("own:pvoffer:" in callback for _, callback in buttons)
        assert ("⬅️ Admin panel", "own:dashboard") in buttons
        assert not any(callback.startswith(("buy:", "cat:", "offer:"))
                       for _, callback in buttons)
        assert not any(callback.startswith("own:pvmethod") for _, callback in buttons)
        content, keyboard = preview_catalog_screen(fixture.offers, currency="USD")
        assert "8.50 USD" not in content  # numeric output trims trailing zero
        assert "8.5 USD" in content
        assert "999 RUB" not in content
        assert any(cb == "own:pvoffer:" + encode_uuid(fixture.usd_price) + ":0"
                   for _, cb in _buttons(keyboard))
    finally:
        _language.reset(token)


def test_preview_plan_and_method_buttons_cannot_invoke_real_checkout():
    fixture = _offers()
    token = _language.set("en")
    try:
        text, keyboard = preview_offer_screen(
            fixture.offers, price_id=fixture.usd_price,
        )
        assert "Subscription checkout" in text
        assert "8.5 USD" in text
        assert "Bank & Co" in str(_buttons(keyboard))
        method_callback = (
            "own:pvmethod:" + encode_uuid(fixture.usd_price)
            + ":" + encode_uuid(fixture.usd_method) + ":0"
        )
        assert len(method_callback) <= 64
        assert any(cb == method_callback for _, cb in _buttons(keyboard))
        assert not any(cb.startswith("buy:") for _, cb in _buttons(keyboard))
        assert not any(cb.startswith("own:pvmethod:") and "RUB" in cb
                       for _, cb in _buttons(keyboard))
        assert preview_offer_screen(fixture.offers, price_id=uuid4()) is None

        rendered = preview_method_screen(
            fixture.offers, price_id=fixture.usd_price,
            payment_method_id=fixture.usd_method,
            instruction_template="Pay {amount} {currency} for {plan} <Bank & Co>",
        )
        assert rendered is not None
        text, keyboard = rendered
        assert "Pay 8.5 USD for 1 месяц &lt;Bank &amp; Co&gt;" in text
        assert "No payment request" in text
        assert any(cb.startswith("own:pvoffer:") for _, cb in _buttons(keyboard))
        assert not any(cb.startswith("buy:") for _, cb in _buttons(keyboard))
        assert preview_method_screen(
            fixture.offers, price_id=fixture.usd_price,
            payment_method_id=fixture.rub_method,
            instruction_template="Bank",  # not eligible for USD
        ) is None
    finally:
        _language.reset(token)


def test_no_sellable_plans_uses_real_buyer_empty_catalog_not_published_titles():
    token = _language.set("en")
    try:
        text, keyboard = preview_catalog_screen(())
        assert "No plans are available for purchase" in text
        assert "No payment request" in text
        assert all(not cb.startswith("own:pvoffer:")
                   for _, cb in _buttons(keyboard))
    finally:
        _language.reset(token)


async def test_owner_can_navigate_preview_without_creating_payment(monkeypatch):
    fixture = _offers()
    actor = uuid4()
    monkeypatch.setattr(
        owner_ui, "ensure_telegram_actor", AsyncMock(return_value=actor),
    )
    check_owner = AsyncMock(return_value=True)
    monkeypatch.setattr(owner_ui, "can_manage_project", check_owner)
    monkeypatch.setattr(
        owner_ui, "get_selected_project",
        AsyncMock(return_value=fixture.project_id),
    )
    get_offers = AsyncMock(return_value=fixture.offers)
    monkeypatch.setattr(owner_ui, "list_buyer_offers", get_offers)
    method = SimpleNamespace(instruction_template="Pay {amount} {currency}")
    session = MagicMock()
    session.scalar = AsyncMock(return_value=method)

    @asynccontextmanager
    async def read_scope(*_args, **_kwargs):
        yield session

    scope = MagicMock(side_effect=read_scope)
    monkeypatch.setattr(owner_ui, "system_project_session_scope", scope)

    router = owner_ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=fixture.project_id,
    )
    callback = next(x.callback for x in router.callback_query.handlers
                    if x.callback.__name__ == "owner_callback")
    query = SimpleNamespace(
        data="own:preview", message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=401), message_id=15,
        ),
        from_user=SimpleNamespace(id=401, username="owner", first_name="Owner"),
        answer=AsyncMock(),
    )
    bot = MagicMock()
    bot.id = 300
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()

    token = _language.set("en")
    try:
        await callback(query, bot)
        assert "Buyer preview" in bot.send_message.await_args.args[1]
        bot.edit_message_text.assert_not_awaited()

        query.data = "own:pvcat:USD:0"
        await callback(query, bot)
        assert "8.5 USD" in bot.edit_message_text.await_args.args[0]

        query.data = "own:pvoffer:" + encode_uuid(fixture.usd_price) + ":0"
        await callback(query, bot)
        assert "Bank & Co" in str(
            _buttons(bot.edit_message_text.await_args.kwargs["reply_markup"])
        )

        query.data = (
            "own:pvmethod:" + encode_uuid(fixture.usd_price)
            + ":" + encode_uuid(fixture.usd_method) + ":0"
        )
        await callback(query, bot)
        assert "Pay 8.5 USD" in bot.edit_message_text.await_args.args[0]
        assert session.scalar.await_count == 1
        assert scope.call_count == 4
        assert get_offers.await_count == 4
        assert bot.send_message.await_count == 1
        session.add.assert_not_called()
        session.flush.assert_not_called()
        assert query.answer.await_count == 4

        query.data = (
            "own:pvmethod:" + encode_uuid(fixture.usd_price)
            + ":" + encode_uuid(fixture.rub_method) + ":0"
        )
        await callback(query, bot)
        assert query.answer.await_args.kwargs["show_alert"] is True
        assert session.scalar.await_count == 1  # method not eligible for USD

        # A large valid template stays in the owner-only callback namespace.
        method.instruction_template = "&" * 4000
        query.data = (
            "own:pvi:" + encode_uuid(fixture.usd_price)
            + ":" + encode_uuid(fixture.usd_method) + ":0:1"
        )
        await callback(query, bot)
        assert "Instructions · part 2/" in bot.edit_message_text.await_args.args[0]
        assert len(bot.edit_message_text.await_args.args[0]) <= 4096
        assert session.scalar.await_count == 2
    finally:
        _language.reset(token)


async def test_preview_requires_connected_owner_permission(monkeypatch):
    fixture = _offers()
    monkeypatch.setattr(
        owner_ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    deny = AsyncMock(return_value=False)
    monkeypatch.setattr(owner_ui, "can_manage_project", deny)
    select_project = AsyncMock()
    monkeypatch.setattr(owner_ui, "get_selected_project", select_project)
    router = owner_ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=fixture.project_id,
    )
    callback = next(x.callback for x in router.callback_query.handlers
                    if x.callback.__name__ == "owner_callback")
    query = SimpleNamespace(
        data="own:pvcat:USD:0", message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=500), message_id=10,
        ),
        from_user=SimpleNamespace(id=500, username="other", first_name="Other"),
        answer=AsyncMock(),
    )
    bot = MagicMock()
    bot.id = 300
    bot.send_message = AsyncMock()
    await callback(query, bot)
    assert query.answer.await_args.kwargs["show_alert"] is True
    select_project.assert_not_awaited()
    bot.send_message.assert_not_awaited()


def test_max_allowed_instruction_is_paginated_without_broken_html_entities():
    fixture = _offers()
    # 4,000 input chars is accepted by owner setup, and HTML escaping grows
    # this further. Every Telegram screen must stay below the 4,096 limit.
    instruction = "&" * 4000
    collected = []
    language = _language.set("en")
    try:
        for part in range(30):
            rendered = preview_method_screen(
                fixture.offers, price_id=fixture.usd_price,
                payment_method_id=fixture.usd_method,
                instruction_template=instruction,
                instruction_page=part,
            )
            if rendered is None:
                break
            text, keyboard = rendered
            assert len(text) <= 4096
            buttons = _buttons(keyboard)
            assert all(len(callback.encode("utf-8")) <= 64
                       for _, callback in buttons)
            assert all(not callback.startswith("buy:")
                       for _, callback in buttons)
            assert "&amp;" in text
            assert "&am\n" not in text
            assert "No payment request" in text
            body = text.split("Method: Bank &amp; Co\n\n", 1)[1]
            body = body.split("\n\n<i>Instructions · part ", 1)[0]
            body = body.split("\n\n<i>Preview only.", 1)[0]
            collected.append(body)
            if part == 0:
                assert any(cb.startswith("own:pvi:") and cb.endswith(":1")
                           for _, cb in buttons)
        assert 2 <= len(collected) < 30
        assert "".join(collected) == "&amp;" * 4000
        assert preview_method_screen(
            fixture.offers, price_id=fixture.usd_price,
            payment_method_id=fixture.usd_method,
            instruction_template=instruction,
            instruction_page=1000,
        ) is None
    finally:
        _language.reset(language)
