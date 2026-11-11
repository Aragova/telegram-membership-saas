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
    assert "<table bordered compact>" in rich
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


def test_missing_monthly_rate_never_claims_a_payable_total():
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
    assert "Итог к оплате не рассчитан" in text
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
    ("ru", "нет курса месяца"), ("en", "monthly rate missing"),
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
