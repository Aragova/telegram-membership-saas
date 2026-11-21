"""Rich/classic central per-bot bill from real bill and read-only previews."""
from copy import deepcopy

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup.bot_bill_ui import (
    bot_bill_classic,
    bot_bill_rich,
)
from telegram_membership_saas.rich_messages import rich_row_marker

TRIAL_BILL = {
    "project_id": "internal-project-id-never-render",
    "bot_id": "internal-bot-id-never-render",
    "invoice_id": None,
    "total_due_rub": "0",
    "upcoming_at": "2099-11-24T18:00:00+03:00",
    "turnover": [],
}
TRIAL = {
    "state": "trial",
    "remaining_seconds": 56 * 3600,
    "trial_ends_at": "2099-11-24T18:00:00+03:00",
}
SALES = {
    "month": "2026-11-01",
    "billing_enabled": True,
    "rails": [
        {"kind": "subscription", "sold": 4},
        {"kind": "file_manual", "sold": 2},
    ],
}
CURRENCIES = {
    "rows": [
        {"currency": "RUB", "gross": "2800.00", "fee_rub": "0.00"},
    ],
}
ISSUED = {
    "project_id": "internal-project-id-never-render",
    "bot_id": "internal-bot-id-never-render",
    "invoice_id": "private-invoice-id-never-render",
    "period_start": "2026-12-16T10:00:00+03:00",
    "period_end": "2027-01-16T10:00:00+03:00",
    "first_period": False,
    "due_at": "2099-01-16T00:00:00+03:00",
    "turnover": [
        {"kind": "subscription", "currency": "RUB",
         "sold": 10, "gross": "6000"},
        {"kind": "file", "currency": "RUB",
         "sold": 5, "gross": "2000"},
    ],
    "service_rub": "150", "subscription_rub": "300", "file_rub": "100",
    "total_rub": "550", "paid_rub": "0", "credit_rub": "0",
    "total_due_rub": "550",
}


def render(lang, bill, *, trial=None, sales=None, currencies=None):
    args = dict(
        username="vimitest21bot",
        bill=bill,
        trial=trial,
        timezone="Europe/Moscow",
        sales=sales,
        currencies=currencies,
        future_currency="RUB",
    )
    token = _language.set(lang)
    try:
        rich = bot_bill_rich(**args)
        classic = bot_bill_classic(**args)
    finally:
        _language.reset(token)
    return rich, classic


@pytest.mark.parametrize("lang,heading,period,sales", [
    ("ru", "СЧЁТ БОТА", "ПРОБНЫЙ ПЕРИОД", "ПРОДАЖИ ТЕКУЩЕГО МЕСЯЦА"),
    ("en", "BOT BILL", "FREE TRIAL", "SALES THIS CALENDAR MONTH"),
])
def test_active_trial_is_true_to_database_and_has_no_payable_charge(
    lang, heading, period, sales,
):
    rich, classic = render(
        lang, TRIAL_BILL, trial=TRIAL, sales=SALES, currencies=CURRENCIES,
    )
    for text in (rich, classic):
        assert heading in text
        assert "@vimitest21bot" in text
        assert "2" in text  # two days of recorded trial time
        assert sales in text
        assert "4" in text and "2" in text  # source-derived sale counts
        assert "2800" in text  # real native RUB turnover, not guessed FX
        assert "150 ₽" in text  # only a future service-rate disclosure
        assert "0 ₽" in text  # no issued invoice, no current amount due
        assert "internal-project-id" not in text
        assert "internal-bot-id" not in text
        assert "private-invoice-id" not in text
        assert "300.00 ₽" not in text  # no invented trial commission
    assert period in rich
    assert "tg-time" in rich or "24.11.2099" in rich
    assert "tg-time" in classic or "24.11.2099" in classic
    assert rich_row_marker(0) in rich  # the single Back action row


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_issued_subsequent_bill_shows_authoritative_service_and_commission(lang):
    rich, classic = render(lang, ISSUED)
    for text in (rich, classic):
        assert "@vimitest21bot" in text
        assert "150.00 ₽" in text and "550.00 ₽" in text
        assert "300.00 ₽" in text and "100.00 ₽" in text
        assert "6000 RUB" in text and "2000 RUB" in text
        assert "private-invoice-id" not in text
        assert ("Продажи подписок" if lang == "ru" else
                "Subscription sales") in text
        assert ("Продажи файлов" if lang == "ru" else
                "File sales") in text
        assert "ПРОБНЫЙ ПЕРИОД" not in text
    assert rich_row_marker(0) in rich
    assert rich_row_marker(1) in rich  # Pay + Back rows


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_first_issued_month_is_service_only_and_sales_are_informational(lang):
    bill = deepcopy(ISSUED)
    bill.update(
        first_period=True, turnover=[], subscription_rub="0", file_rub="0",
        total_rub="150", total_due_rub="150",
    )
    rich, classic = render(
        lang, bill, sales=SALES, currencies=CURRENCIES,
    )
    for text in (rich, classic):
        assert "150.00 ₽" in text
        assert "550.00 ₽" not in text
        assert "300.00 ₽" not in text
        assert "2800" in text
        assert "4" in text
        assert "2026-11" in text
        assert ("не входят" if lang == "ru" else "not part") in text
        assert "private-invoice-id" not in text


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_old_trial_without_invoice_is_pending_not_new_trial(lang):
    ended = {"state": "ended", "remaining_seconds": 0}
    bill = dict(TRIAL_BILL, upcoming_at="2020-01-01T12:00:00+03:00")
    rich, classic = render(lang, bill, trial=ended)
    for text in (rich, classic):
        assert "ПРОБНЫЙ ПЕРИОД" not in text
        assert "FREE TRIAL" not in text
        assert "2020" not in text  # past trial deadline is not future due date
        assert ("Ожидается выставление счёта" if lang == "ru" else
                "invoice issuance is pending") in text
        assert "⚠️" in text  # unavailable sales explicitly flagged


def test_missing_trial_or_sales_never_fabricates_figures():
    rich, classic = render("ru", TRIAL_BILL)
    for text in (rich, classic):
        assert "Статус пробного периода временно недоступен" in text
        assert "Статистика продаж временно недоступна" in text
        assert "4 покупок" not in text
        assert "2800" not in text
        assert "ПРОБНЫЙ ПЕРИОД" not in text


def test_escaped_owner_name_and_native_currencies():
    args = dict(
        username="test<&bot",
        bill=TRIAL_BILL,
        trial=TRIAL,
        timezone="Europe/Moscow",
        sales=SALES,
        currencies={"rows": [
            {"currency": "XTR", "gross": "250", "fee_rub": "0"},
        ]},
        future_currency="USD",
    )
    rich = bot_bill_rich(**args)
    classic = bot_bill_classic(**args)
    for text in (rich, classic):
        assert "@test&lt;&amp;bot" in text
        assert "@test<&bot" not in text
        assert "250" in text
        assert "Telegram Stars" in text
        assert "$5" in text
