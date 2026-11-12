"""One connected-bot bill screen: no visible FX table, native Stars rows."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup.handlers import (
    _saas_invoices_rich,
    _saas_preview_classic,
    _saas_preview_rich,
    _saas_stars_rich,
)

BASE = {
    "month": "2026-11-01",
    "rows": [
        {"currency": "RUB", "gross": "1000", "fee_rub": "50",
         "missing": 0, "operations": 5},
        {"currency": "USD", "gross": "50", "fee_rub": "200",
         "missing": 0, "operations": 2},
    ],
    "fiat_commission_rub": "250",
    "stars_need_reconciliation": False, "missing_daily_rates": 0,
    "next_bot_fee_rub": "150", "is_platform_owner": False,
    "billing_enabled": False, "estimate_complete": True,
    "rate_date": "2026-11-11",
    "today_rates": {
        "USD": {"rate": "80.5573", "source_date": "2026-11-11"},
        "EUR": {"rate": "89.6603", "source_date": "2026-11-11"},
    },
}


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_bill_preview_no_exposed_fx_rates_or_fake_issued_invoice(lang):
    token = _language.set(lang)
    try:
        rich, classic = _saas_preview_rich(BASE), _saas_preview_classic(BASE)
    finally:
        _language.reset(token)
    assert rich.count("<table bordered compact>") == 2
    assert "80.5573" not in rich + classic
    assert "89.6603" not in rich + classic
    assert "Официальные курсы" not in rich
    assert "official exchange rates" not in rich
    assert "5%" in rich and "USD" in rich
    assert "400.00" in rich
    assert "400.00" not in classic
    assert "<table" not in classic and "<h3>" not in classic
    assert ("Биллинг" in rich if lang == "ru" else "Billing" in rich)


def test_stars_separate_from_fiat_payable_total():
    data = dict(BASE, rows=[
        {"currency": "XTR", "gross": 500, "fee_rub": "30",
         "missing": 0, "operations": 1},
    ], fiat_commission_rub=0, next_bot_fee_rub=150,
        stars_need_reconciliation=True, estimate_complete=False)
    token = _language.set("ru")
    try:
        text = _saas_preview_rich(data)
    finally:
        _language.reset(token)
    assert "500" in text
    assert "В отдельной таблице" in text
    assert "не включается в итоговую комиссию" in text
    assert "Предварительно:" not in text


def test_missing_sale_day_rate_never_claims_payable_total():
    data = dict(BASE, rows=[
        {"currency": "USD", "gross": 50, "fee_rub": 0,
         "missing": 1, "operations": 1},
    ], fiat_commission_rub=0, missing_daily_rates=1,
        estimate_complete=False)
    token = _language.set("ru")
    try:
        rich = _saas_preview_rich(data)
        classic = _saas_preview_classic(data)
    finally:
        _language.reset(token)
    assert "Нет курса" in rich
    assert "Полная сумма не рассчитана" in rich
    assert "нет курса" in classic
    assert "Итог не готов" in classic


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_classic_monthly_view_never_exposes_sale_date_rate_range(lang):
    data = dict(BASE, rows=[
        {"currency": "USD", "gross": 80, "fee_rub": 335,
         "min_rate": 80, "max_rate": 90, "missing": 0, "operations": 2},
    ])
    token = _language.set(lang)
    try:
        result = _saas_preview_classic(data)
    finally:
        _language.reset(token)
    assert "80 USD" in result
    assert "5%: 335 ₽" in result
    assert "80–90" not in result


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_stars_price_grouped_table_without_file_names(lang):
    stars = {
        "stars": 2000, "sold": 8,
        "reward_usd": "26.000000000", "commission_usd": "1.300000000",
        "pending_reconciliation": 0,
        "rows": [
            {"source": "file_stars", "price_stars": 100,
             "sold": 5, "total_stars": 500},
            {"source": "file_stars", "price_stars": 500,
             "sold": 3, "total_stars": 1500},
        ],
    }
    token = _language.set(lang)
    try:
        html = _saas_stars_rich(stars)
    finally:
        _language.reset(token)
    assert "100 ⭐ Telegram Stars" in html and "500 ⭐ Telegram Stars" in html
    assert "2000" in html and "$1.30" in html
    assert "JPEG" not in html
    assert "<table bordered compact>" in html


@pytest.mark.parametrize("lang,expected", [
    ("ru", "⭐ Telegram Stars · Возвраты"),
    ("en", "⭐ Telegram Stars · Refunds"),
])
def test_real_stars_refunds_show_separate_unapplied_credits(lang, expected):
    stars = {
        "stars": 2000, "sold": 1, "reward_usd": "26",
        "commission_usd": "1.3",
        "rows": [{"source": "file_stars", "price_stars": 2000,
                  "sold": 1, "total_stars": 2000}],
        "refunds": 1, "refunded_stars": 2000,
        "reward_credit_usd": "26",
        "commission_credit_usd": "1.3",
        "pending_refund_reconciliation": 0,
        "pending_reconciliation": 0,
    }
    token = _language.set(lang)
    try:
        html = _saas_stars_rich(stars)
    finally:
        _language.reset(token)
    assert expected in html
    assert "$1.30" in html
    assert "2000 ⭐ Telegram Stars" in html
    assert "Credits are not yet applied" in html if lang == "en" else (
        "Кредиты ещё не зачтены" in html
    )


def test_saas_invoice_counts_are_separate_from_turnover():
    report = SimpleNamespace(
        total=0, unpaid=0, outstanding_rub=Decimal("0"),
        invoices=[], configured=False, instructions=None,
    )
    token = _language.set("ru")
    try:
        html = _saas_invoices_rich(report)
    finally:
        _language.reset(token)
    assert "<h4>📑 Счета платформы</h4>" in html
    assert "<table bordered compact>" in html
    assert "Счетов к оплате пока нет." in html
