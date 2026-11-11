"""Connected-bot admin Bill preview, one existing navigation path and Rich fallback."""

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup.handlers import (
    _saas_preview_classic,
    _saas_preview_rich,
)


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_bill_preview_shows_original_currencies_and_estimate_not_invoice(lang):
    data = {
        "month": "2026-11-01",
        "rows": [
            {"currency": "RUB", "gross": "1000", "fee_rub": "50",
             "missing": 0, "operations": 1},
            {"currency": "USD", "gross": "50", "fee_rub": "200",
             "missing": 0, "operations": 1},
            {"currency": "EUR", "gross": "30", "fee_rub": "135",
             "missing": 0, "operations": 1},
        ],
        "fiat_commission_rub": "385", "stars_estimate_rub": "0",
        "stars_need_reconciliation": False, "missing_daily_rates": 0,
        "next_bot_fee_rub": "150", "is_platform_owner": False,
        "billing_enabled": False, "estimate_complete": True,
    }
    token = _language.set(lang)
    try:
        rich = _saas_preview_rich(data)
        classic = _saas_preview_classic(data)
    finally:
        _language.reset(token)
    assert rich.count("<table bordered compact>") >= 2
    assert "Официальные курсы на сегодня" in rich if lang == "ru" else "Today's official exchange rates" in rich
    assert "1000" in rich and "50" in rich and "30" in rich
    assert "535.00" in rich
    assert "535.00" not in classic  # simplified safe HTML fallback
    assert "<table" not in classic
    assert "<h3>" not in classic
    assert "150" in classic
    assert ("Биллинг" in rich if lang == "ru" else "Billing" in rich)


def test_stars_requires_refunds_and_reward_value_before_invoicing():
    data = {
        "month": "2026-11-01",
        "rows": [
            {"currency": "XTR", "gross": 500, "fee_rub": "30",
             "missing": 0, "operations": 1},
        ],
        "fiat_commission_rub": 0, "stars_estimate_rub": "30",
        "stars_need_reconciliation": True,
        "missing_daily_rates": 0, "next_bot_fee_rub": 150,
        "billing_enabled": False, "is_platform_owner": False,
        "estimate_complete": False,
    }
    token = _language.set("ru")
    try:
        result = _saas_preview_rich(data)
    finally:
        _language.reset(token)
    assert "500" in result
    assert "30" in result
    assert "сумма не входит в счёт" in result
    assert "Предварительно:" not in result


def test_missing_sale_day_rate_never_claims_a_payable_total():
    data = {
        "month": "2026-11-01",
        "rows": [{"currency": "USD", "gross": 50, "fee_rub": 0,
                  "missing": 1, "operations": 1}],
        "fiat_commission_rub": 0, "stars_estimate_rub": 0,
        "stars_need_reconciliation": False, "missing_daily_rates": 1,
        "next_bot_fee_rub": 150, "billing_enabled": False,
        "is_platform_owner": False, "estimate_complete": False,
    }
    token = _language.set("ru")
    try:
        text = _saas_preview_rich(data)
    finally:
        _language.reset(token)
    assert "Нет курса" in text
    assert "Полная сумма не рассчитана" in text
    assert "Предварительно:" not in text


@pytest.mark.parametrize("lang,rate_label", [
    ("ru", "курс: "), ("en", "rate: "),
])
def test_classic_bill_preserves_rate_range_for_old_or_ambiguous_data(
    lang, rate_label,
):
    """Two approved payment days cannot be represented by the first rate."""
    data = {
        "month": "2026-11-01",
        "rows": [
            {"currency": "USD", "gross": "80", "fee_rub": "335",
             "min_rate": 80, "max_rate": 90, "missing": 0, "operations": 2},
            {"currency": "EUR", "gross": "30", "fee_rub": "135",
             "min_rate": 90, "max_rate": 90, "missing": 0, "operations": 1},
        ],
        "fiat_commission_rub": "470",
        "stars_estimate_rub": 0,
        "stars_need_reconciliation": False,
        "missing_daily_rates": 0,
        "next_bot_fee_rub": 150,
        "billing_enabled": False,
        "is_platform_owner": False,
        "estimate_complete": True,
    }
    token = _language.set(lang)
    try:
        classic = _saas_preview_classic(data)
    finally:
        _language.reset(token)
    assert "80 USD" in classic
    assert rate_label + "80–90 ₽" in classic
    assert "5%: 335 ₽" in classic
    assert rate_label + "90 ₽" in classic
    assert "<table" not in classic


@pytest.mark.parametrize("lang,missing_text", [
    ("ru", "нет курса даты продажи"), ("en", "sale-date rate missing"),
])
def test_classic_bill_does_not_invent_rate_when_one_day_unrated(
    lang, missing_text,
):
    data = {
        "month": "2026-11-01",
        "rows": [
            {"currency": "USD", "gross": "80", "fee_rub": 200,
             "min_rate": 80, "max_rate": 80, "missing": 1, "operations": 2},
        ],
        "fiat_commission_rub": 200,
        "stars_estimate_rub": 0,
        "stars_need_reconciliation": False,
        "missing_daily_rates": 1,
        "next_bot_fee_rub": 150,
        "billing_enabled": False,
        "is_platform_owner": False,
        "estimate_complete": False,
    }
    token = _language.set(lang)
    try:
        classic = _saas_preview_classic(data)
    finally:
        _language.reset(token)
    assert missing_text in classic
    assert ("Итог не готов" if lang == "ru" else "No final amount") in classic


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_daily_rich_cbr_rates_are_a_table_with_dates_not_one_inline_dump(lang):
    data = {
        "month": "2026-11-01",
        "rows": [],
        "fiat_commission_rub": 0,
        "next_bot_fee_rub": 150,
        "stars_need_reconciliation": False,
        "stars_estimate_rub": 0,
        "missing_daily_rates": 0,
        "estimate_complete": True,
        "billing_enabled": False,
        "is_platform_owner": False,
        "rate_date": "2026-11-11",
        "today_rates": {
            "USD": {"rate": "80.5573", "source_date": "2026-11-11"},
            "EUR": {"rate": "89.6603", "source_date": "2026-11-11"},
            "UAH": {"rate": "1.76935", "source_date": "2026-11-11"},
            "BYN": {"rate": "27.3446", "source_date": "2026-11-11"},
        },
    }
    token = _language.set(lang)
    try:
        html = _saas_preview_rich(data)
    finally:
        _language.reset(token)
    assert "<table bordered compact>" in html
    assert "USD</td><td>80.5573</td>" in html
    assert "EUR</td><td>89.6603</td>" in html
    assert "BYN</td><td>27.3446</td>" in html
    assert html.count("<tr><td>") >= 6
    assert "Нет продаж" not in html
    assert "80.5573 ₽, EUR" not in html


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_currency_quote_rich_uses_separate_settlement_day(lang):
    from telegram_membership_saas.modules.owner_setup.handlers import (
        _saas_currency_quote_rich,
    )

    quote = {
        "chosen_currency": "EUR", "billing_month": "2026-11-01",
        "quote_day": "2026-11-11", "rate_rub_per_unit": "89.6603",
        "total_rub": 150, "estimated_total": "1.67",
        "quote_ready": True, "source_name": "CBR",
        "source_date": "2026-11-11",
    }
    token = _language.set(lang)
    try:
        html = _saas_currency_quote_rich(quote)
    finally:
        _language.reset(token)
    assert "<h3>" in html and "<table bordered compact>" in html
    assert "89.6603" in html and "2026-11-11" in html
    assert "1.67 EUR" in html
