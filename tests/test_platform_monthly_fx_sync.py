"""Official monthly foreign-exchange parser: no invented or mis-scaled rates."""
from datetime import date
from decimal import Decimal

import pytest

from telegram_membership_saas.modules.platform_monthly_fx_sync import (
    parse_official_rates,
)


def _sample(*, date_str="01.11.2026", missing=None, amount="800,00"):
    currencies = [
        ("USD", "1", "80,00"),
        ("EUR", "1", "90,00"),
        ("UAH", "10", "210,00"),
        ("BYN", "1", "26,50"),
    ]
    body = "".join(
        "<Valute><CharCode>" + code + "</CharCode>"
        "<Nominal>" + nominal + "</Nominal>"
        "<Value>" + (amount if code == "USD" else price)
        + "</Value></Valute>"
        for code, nominal, price in currencies if code != missing
    )
    return (
        '<ValCurs Date="' + date_str + '" name="Foreign Currency Market">'
        + body + "</ValCurs>"
    ).encode()


def test_monthly_cbr_parser_honors_nominals_and_decimal_comma():
    quoted, rates = parse_official_rates(
        _sample(amount="80,00"), month=date(2026, 11, 1),
    )
    assert quoted == date(2026, 11, 1)
    assert rates == {
        "USD": Decimal("80"),
        "EUR": Decimal("90"),
        "UAH": Decimal("21"),  # Official quote is for 10 hryvnias.
        "BYN": Decimal("26.50"),
    }


@pytest.mark.parametrize("payload", [
    _sample(missing="UAH"),
    _sample(date_str="02.11.2026"),
    _sample(date_str="01.01.2026"),
    b"<html>provider unavailable</html>",
    b"<ValCurs>",
    _sample(amount="-80,00"),
    _sample(amount="oops"),
])
def test_monthly_cbr_parser_fails_closed_on_incomplete_or_untrusted_data(payload):
    with pytest.raises((ValueError, Exception)):
        parse_official_rates(payload, month=date(2026, 11, 1))


def test_supervisor_receives_same_owner_identity_as_central_bot():
    """CI regression: Compose must not silently disable official FX sync."""
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text(
        encoding="utf-8",
    )
    central = compose.split("\n  bot:\n", 1)[1].split(
        "\n  customer-supervisor:\n", 1,
    )[0]
    supervisor = compose.split("\n  customer-supervisor:\n", 1)[1].split(
        "\n  customer-bot:\n", 1,
    )[0]
    expected = (
        "TMS_PLATFORM_OWNER_TELEGRAM_ID: "
        "${TMS_PLATFORM_OWNER_TELEGRAM_ID:-}"
    )
    assert expected in central
    assert expected in supervisor


async def test_supervisor_monthly_fx_failure_retries_quickly_without_leaking(
    monkeypatch, caplog,
):
    from unittest.mock import AsyncMock, MagicMock

    from telegram_membership_saas import customer_supervisor as runtime
    from telegram_membership_saas.config import Settings
    from telegram_membership_saas.customer_supervisor import CustomerBotSupervisor

    simulated_secret = "private-provider-token-must-not-appear"
    fetch = AsyncMock(side_effect=RuntimeError(simulated_secret))
    monkeypatch.setattr(runtime, "sync_official_month_rates", fetch)
    monkeypatch.setattr(runtime, "monotonic", lambda: 1000.0)
    supervisor = CustomerBotSupervisor(
        MagicMock(), MagicMock(),
        Settings(
            platform_owner_telegram_id=123456789,
            telegram_central_access_enabled=False,
            _env_file=None,
        ),
    )
    await supervisor._sync_monthly_fx_safe()
    fetch.assert_awaited_once()
    assert 1290 <= supervisor._next_fx_sync_at <= 1310
    assert "FX sync failed (RuntimeError)" in caplog.text
    assert simulated_secret not in caplog.text
