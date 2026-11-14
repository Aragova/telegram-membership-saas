"""Issued per-bot Rich statement and first activation without commissions."""

from copy import deepcopy

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.platform_saas_bill_ui import (
    unified_merchant_bill_classic,
    unified_merchant_bill_rich,
)

BILL = {
    "bot_id": "private-bot", "project_id": "private-project",
    "invoice_id": "never-expose-uuid",
    "period_start": "2026-12-16T10:00:00+03:00",
    "period_end": "2027-01-16T10:00:00+03:00",
    "first_period": False,
    "turnover": [
        {"kind": "subscription", "currency": "RUB",
         "sold": 20, "gross": "10000.00"},
        {"kind": "subscription", "currency": "XTR",
         "sold": 2, "gross": "250"},
        {"kind": "file", "currency": "USD",
         "sold": 10, "gross": "100.00"},
    ],
    "service_rub": "150.00", "subscription_rub": "500.00",
    "file_rub": "200.00", "total_rub": "850.00",
    "paid_rub": "0.00", "credit_rub": "0.00",
    "total_due_rub": "850.00",
}


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_one_bot_monthly_bill_includes_service_and_both_sales(lang):
    token = _language.set(lang)
    try:
        rich = unified_merchant_bill_rich(BILL)
        classic = unified_merchant_bill_classic(BILL)
    finally:
        _language.reset(token)
    for view in (rich, classic):
        assert "10000 RUB" in view
        assert "250 ⭐ Telegram Stars" in view
        assert "100 USD" in view
        assert "150.00 ₽" in view and "850.00 ₽" in view
        assert "16.12.2026" in view
        assert "never-expose-uuid" not in view
        assert ("Продажи подписок" if lang == "ru"
                else "Subscription sales") in view
        assert ("Продажи файлов" if lang == "ru"
                else "File sales") in view
    assert rich.count("<table bordered compact>") == 2
    assert "<table" not in classic


def test_first_month_contains_only_service_and_full_pay():
    first = deepcopy(BILL)
    first.update(first_period=True, turnover=[], subscription_rub="0",
                 file_rub="0", total_rub="150", total_due_rub="150")
    token = _language.set("ru")
    try:
        rich = unified_merchant_bill_rich(first)
    finally:
        _language.reset(token)
    assert "Абонплата за бота" in rich and "150.00 ₽" in rich
    assert "Продажи файлов" not in rich
    assert "Продажи подписок" not in rich


def test_paid_and_refund_credit_are_separate_from_amount_due():
    bill = deepcopy(BILL)
    bill.update(paid_rub="600", credit_rub="100", total_due_rub="150")
    html = unified_merchant_bill_classic(bill)
    assert "600.00 ₽" in html and "100.00 ₽" in html
    assert "150.00 ₽" in html


def test_inconsistent_invoice_totals_fail_closed():
    bill = deepcopy(BILL)
    bill["total_due_rub"] = "851"
    with pytest.raises(ValueError, match="Inconsistent"):
        unified_merchant_bill_rich(bill)


def test_trial_has_no_issued_invoice_or_payable_amount():
    bill = deepcopy(BILL)
    bill.update(invoice_id=None, turnover=[], total_due_rub="0")
    text = unified_merchant_bill_rich(bill)
    assert "no invoice" in text.lower() or "Счёт ещё не выставлен" in text
    assert "850.00 ₽" not in text
