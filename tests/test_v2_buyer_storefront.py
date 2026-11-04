"""Approved v2 buyer UX: real currency-specific offers, never side-effectful navigation."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.telegram_commerce import handlers as ui
from telegram_membership_saas.modules.telegram_commerce.service import (
    CheckoutOption,
    encode_uuid,
)
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    PAGE_SIZE,
    available_currencies,
    build_buyer_offers,
    catalog_screen,
    offer_screen,
)


def _option(
    plan_id, price_id, method_id, *, code="RUB", name="Premium",
    period=1, unit="month", amount="10", method="СБП",
):
    return CheckoutOption(
        plan_id=plan_id, plan_name=name, duration_value=period,
        duration_unit=unit, plan_price_id=price_id, currency_code=code,
        amount=Decimal(amount), currency_precision=2,
        payment_method_id=method_id, payment_method_name=method,
    )


def _buttons(keyboard):
    return [
        button for row in keyboard.inline_keyboard for button in row
    ]


def _catalog():
    plan = uuid4()
    rub = uuid4()
    usd = uuid4()
    sbp = uuid4()
    bank = uuid4()
    paypal = uuid4()
    offers = build_buyer_offers(
        (
            _option(plan, rub, sbp, code="RUB", method="СБП"),
            _option(plan, rub, bank, code="RUB", method="МИР"),
            _option(plan, usd, paypal, code="USD", amount="7", method="PayPal"),
        ),
        {plan: ("VIP channel",)},
    )
    return offers, rub, usd, sbp, bank, paypal


def test_catalog_collapses_many_methods_into_one_plan_per_currency():
    offers, rub, usd, _sbp, _bank, _paypal = _catalog()
    assert len(offers) == 2
    assert available_currencies(offers) == ("RUB", "USD")

    text, keyboard = catalog_screen(offers)
    assert "VIP channel" in text
    assert "10 RUB" in text
    assert "7 USD" not in text
    buttons = _buttons(keyboard)
    assert len([b for b in buttons if b.callback_data.startswith("offer:")]) == 1
    assert f"offer:{encode_uuid(rub)}:0" in [b.callback_data for b in buttons]
    assert "cat:USD:0" in [b.callback_data for b in buttons]

    usd_text, usd_keyboard = catalog_screen(offers, currency="USD")
    assert "7 USD" in usd_text
    assert "10 RUB" not in usd_text
    assert f"offer:{encode_uuid(usd)}:0" in [
        b.callback_data for b in _buttons(usd_keyboard)
    ]


def test_price_detail_shows_only_methods_for_its_currency():
    offers, rub, usd, sbp, bank, paypal = _catalog()
    result = offer_screen(offers, price_id=rub)
    assert result is not None
    text, keyboard = result
    assert "10 RUB" in text
    buttons = _buttons(keyboard)
    callback_data = {b.callback_data for b in buttons}
    assert f"buy:{encode_uuid(rub)}:{encode_uuid(sbp)}" in callback_data
    assert f"buy:{encode_uuid(rub)}:{encode_uuid(bank)}" in callback_data
    assert f"buy:{encode_uuid(rub)}:{encode_uuid(paypal)}" not in callback_data
    assert f"offer:{encode_uuid(usd)}:0" in callback_data
    assert "PayPal" not in [b.text for b in buttons]

    usd_result = offer_screen(offers, price_id=usd)
    assert usd_result is not None
    usd_text, usd_keyboard = usd_result
    assert "7 USD" in usd_text
    methods = [
        b.callback_data for b in _buttons(usd_keyboard)
        if b.callback_data.startswith("buy:")
    ]
    assert methods == [f"buy:{encode_uuid(usd)}:{encode_uuid(paypal)}"]


def test_stale_offer_does_not_reach_payment_selection():
    offers, *_ = _catalog()
    assert offer_screen(offers, price_id=uuid4()) is None


def test_multiple_periods_and_lifetime_are_explicit_without_fx():
    first = uuid4()
    second = uuid4()
    monthly = _option(
        first, uuid4(), uuid4(), name="VIP",
        period=1, unit="month", amount="499",
    )
    lifetime = _option(
        second, uuid4(), uuid4(), name="VIP",
        period=None, unit="lifetime", amount="3999",
    )
    offers = build_buyer_offers(
        (monthly, lifetime), {first: ("VIP channel",), second: ("VIP channel",)},
    )
    text, keyboard = catalog_screen(offers)
    assert text.count("VIP channel") == 1
    assert "1 месяц" in text
    assert "навсегда" in text
    assert "3999 RUB" in text
    assert len([b for b in _buttons(keyboard) if b.callback_data.startswith("offer:")]) == 2


def test_long_catalog_uses_bounded_pages_and_callback_ids_fit_telegram():
    options = tuple(
        _option(uuid4(), uuid4(), uuid4(), name="Level " + str(i))
        for i in range(PAGE_SIZE + 2)
    )
    offers = build_buyer_offers(options)
    first_text, first_keyboard = catalog_screen(offers)
    assert f"Страница 1 из 2" in first_text
    assert "cat:RUB:1" in [b.callback_data for b in _buttons(first_keyboard)]
    second_text, second_keyboard = catalog_screen(offers, page=1)
    assert "Страница 2 из 2" in second_text
    assert len([
        b for b in _buttons(second_keyboard)
        if b.callback_data.startswith("offer:")
    ]) == 2
    assert all(
        len(b.callback_data.encode("utf-8")) <= 64
        for b in _buttons(first_keyboard) + _buttons(second_keyboard)
    )


@pytest.mark.parametrize("requested", ["FAKE", "BYN", "", None])
def test_non_configured_currency_never_creates_new_prices(requested):
    offers, *_ = _catalog()
    text, _ = catalog_screen(offers, currency=requested)
    assert "10 RUB" in text
    assert "0 BYN" not in text


def _callback(data):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(
            id=1337, username="buyer", first_name="Buyer",
        ),
        message=SimpleNamespace(
            message_id=77,
            chat=SimpleNamespace(type="private", id=1337),
        ),
        answer=AsyncMock(),
    )


def _handler(router, name):
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == name
    )


async def test_currency_switch_is_navigation_not_payment(monkeypatch):
    router = ui.create_telegram_commerce_router(MagicMock(), project_id=uuid4())
    rendered = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "_send_catalog", rendered)
    bot = MagicMock()
    callback = _callback("cat:USD:0")
    await _handler(router, "currency_handler")(callback, bot)
    assert rendered.await_args.kwargs["currency"] == "USD"
    assert rendered.await_args.kwargs["message_id"] == 77
    callback.answer.assert_awaited_once()


async def test_offer_click_is_navigation_without_checkout(monkeypatch):
    router = ui.create_telegram_commerce_router(MagicMock(), project_id=uuid4())
    rendered = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "_send_catalog", rendered)
    callback = _callback("offer:" + encode_uuid(uuid4()) + ":0")
    await _handler(router, "offer_handler")(callback, MagicMock())
    assert rendered.await_args.kwargs["price_id"] is not None
    assert rendered.await_args.kwargs["page"] == 0
    callback.answer.assert_awaited_once()


async def test_private_buyer_callback_rejects_foreign_chats_and_stale_ids(monkeypatch):
    router = ui.create_telegram_commerce_router(MagicMock(), project_id=uuid4())
    rendered = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "_send_catalog", rendered)
    callback = _callback("cat:USD:0")
    callback.message.chat.id = 9999
    await _handler(router, "currency_handler")(callback, MagicMock())
    rendered.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True

    callback = _callback("offer:" + encode_uuid(uuid4()) + ":0")
    rendered.return_value = False
    await _handler(router, "offer_handler")(callback, MagicMock())
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_repeated_currency_click_does_not_send_duplicate_message(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    class FakeBot:
        edit_message_text = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(),
                message="Bad Request: message is not modified",
            )
        )
        send_message = AsyncMock()

    offers, *_ = _catalog()
    bot = FakeBot()
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield MagicMock()
    monkeypatch.setattr(ui, "system_project_session_scope", scope)
    monkeypatch.setattr(ui, "list_buyer_offers", AsyncMock(return_value=offers))
    result = await ui._send_catalog(
        bot, chat_id=1337, session_factory=MagicMock(),
        project_id=uuid4(), currency="USD", page=0, message_id=77,
    )
    assert result is True
    bot.send_message.assert_not_awaited()
