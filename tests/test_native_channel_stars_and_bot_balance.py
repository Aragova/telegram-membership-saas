"""Native channel Stars routes stay separate from bot Stars invoices and ledgers."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from telegram_membership_saas.modules.owner_setup.native_channel_stars import (
    NATIVE_PERIOD_SECONDS,
    native_price_allowed,
)
from telegram_membership_saas.modules.owner_setup.statistics import OwnerStatistics
from telegram_membership_saas.modules.telegram_commerce.service import CheckoutOption
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    build_buyer_offers,
    offer_screen,
)
from test_channel_management_ui import _auth, _bot, _handler, _query
from telegram_membership_saas.modules.owner_setup import handlers as owner_ui


@pytest.mark.parametrize(("value", "unit", "amount", "allowed"), [
    (1, "month", "1", True),
    (1, "month", "10000", True),
    (1, "month", "0", False),
    (1, "month", "10001", False),
    (1, "month", "12.5", False),
    (30, "day", "100", False),
    (2, "month", "100", False),
])
def test_native_channel_star_constraints(value, unit, amount, allowed):
    assert native_price_allowed(
        duration_value=value, duration_unit=unit, amount=Decimal(amount),
    ) is allowed
    assert NATIVE_PERIOD_SECONDS == 2_592_000


def test_native_link_is_one_buyer_payment_button_not_a_bot_invoice():
    price_id, plan_id = uuid4(), uuid4()
    option = CheckoutOption(
        plan_id=plan_id, plan_name="Premium", duration_value=1,
        duration_unit="month", plan_price_id=price_id,
        currency_code="XTR", amount=Decimal("50"), currency_precision=0,
        payment_method_id=UUID(int=0), payment_method_name="⭐ Telegram Stars",
    )
    link = "https://t.me/+genuine_link_placeholder"
    offers = build_buyer_offers(
        (option,), native_links={price_id: link},
    )
    _, keyboard = offer_screen(offers, price_id=price_id)
    buttons = [item for row in keyboard.inline_keyboard for item in row]
    assert sum(item.url == link for item in buttons) == 1
    assert not any((item.callback_data or "").startswith("buy:") for item in buttons)

    classic = build_buyer_offers((option,))
    _, regular = offer_screen(classic, price_id=price_id)
    assert any((item.callback_data or "").startswith("buy:") for row in
               regular.inline_keyboard for item in row)


async def test_bot_star_balance_shows_authoritative_zero(monkeypatch):
    project = uuid4()
    _auth(monkeypatch, project)
    stats = OwnerStatistics(
        project_name="Demo", timezone="Europe/Moscow", period="7",
        since=date(2026, 11, 10), through=date(2026, 11, 18),
        payment_count=0, currency_totals=(),
        total_customers=0, new_customers=0,
        active_paid_customers=0, expiring_paid_customers=0,
        active_manual_grants=0, waiting_review=0,
    )
    monkeypatch.setattr(owner_ui, "read_owner_statistics",
                        AsyncMock(return_value=stats))
    send = AsyncMock()
    monkeypatch.setattr(owner_ui, "send_owner_rich", send)
    bot = _bot()
    bot.get_my_star_balance = AsyncMock(
        return_value=SimpleNamespace(amount=0, nanostar_amount=0),
    )
    await _handler(project)(_query("statistics:7"), bot)
    rendered = send.await_args.kwargs
    assert "⭐" in rendered["classic_html"]
    assert "0" in rendered["classic_html"]
    assert "0" in rendered["rich_html"]
    bot.get_my_star_balance.assert_awaited_once_with()


async def test_bot_star_balance_failure_is_not_misreported_as_zero(monkeypatch):
    project = uuid4()
    _auth(monkeypatch, project)
    stats = OwnerStatistics(
        project_name="Demo", timezone="Europe/Moscow", period="7",
        since=date(2026, 11, 10), through=date(2026, 11, 18),
        payment_count=0, currency_totals=(),
        total_customers=0, new_customers=0,
        active_paid_customers=0, expiring_paid_customers=0,
        active_manual_grants=0, waiting_review=0,
    )
    monkeypatch.setattr(owner_ui, "read_owner_statistics",
                        AsyncMock(return_value=stats))
    send = AsyncMock()
    monkeypatch.setattr(owner_ui, "send_owner_rich", send)
    bot = _bot()
    bot.get_my_star_balance = AsyncMock(side_effect=ValueError("unavailable"))
    await _handler(project)(_query("statistics:7"), bot)
    assert "недоступ" in send.await_args.kwargs["classic_html"].lower()
