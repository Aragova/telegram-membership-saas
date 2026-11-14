"""Unified ViMi Pay Rich bill: completed sales by rail, one live payable."""

from copy import deepcopy

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.platform_saas_bill_ui import (
    unified_merchant_bill_classic,
    unified_merchant_bill_rich,
)


SUMMARY = {
    "month": "2026-11-01",
    "project_id": "scoped-bot",
    "turnover": [
        {"kind": "subscription", "currency": "RUB",
         "sold": 20, "gross": "10000.00"},
        {"kind": "subscription", "currency": "XTR",
         "sold": 2, "gross": "250"},
        {"kind": "file", "currency": "EUR",
         "sold": 3, "gross": "30.00"},
        {"kind": "file", "currency": "USD",
         "sold": 10, "gross": "100.00"},
        {"kind": "file", "currency": "BTC",
         "sold": 1, "gross": "0.005"},
    ],
    "service_due_rub": "150.00",
    "commission_due_rub": "700.00",
    "total_due_rub": "850.00",
    "unpaid_count": 2,
    "first_invoice_id": "secret-uuid",
}
PREVIEW = {
    "rails": [
        {"kind": "subscription", "commission_rub": "500.00"},
        {"kind": "subscription_stars", "commission_rub": "15.00"},
        {"kind": "file_manual", "commission_rub": "200.00"},
        {"kind": "file_crypto", "commission_rub": "10.00"},
    ],
    "unrated_billable": 0, "uncaptured_crypto": 0,
}


@pytest.mark.parametrize("language", ["ru", "en"])
def test_bill_one_card_includes_subscription_file_and_service(language):
    token = _language.set(language)
    try:
        rich = unified_merchant_bill_rich(SUMMARY, PREVIEW)
        classic = unified_merchant_bill_classic(SUMMARY, PREVIEW)
    finally:
        _language.reset(token)
    for view in (rich, classic):
        assert "10000 RUB" in view
        assert "250 ⭐ Telegram Stars" in view
        assert "100 USD" in view
        assert "30 EUR" in view
        assert "0.005 BTC" in view
        assert "150.00 ₽" in view
        assert "700.00 ₽" in view
        assert "850.00 ₽" in view
        assert "secret-uuid" not in view
    assert rich.count("<table bordered compact>") == 3
    assert "<table" not in classic
    assert "515.00 ₽" in rich and "210.00 ₽" in rich
    assert ("Подписки" if language == "ru" else "Subscriptions") in rich
    assert ("Файлы" if language == "ru" else "Files") in rich


def test_provisional_sales_never_inflate_payable_account_balance():
    summary = deepcopy(SUMMARY)
    summary.update(service_due_rub="150", commission_due_rub="0",
                   total_due_rub="150")
    preview = deepcopy(PREVIEW)
    preview["unrated_billable"] = 1
    token = _language.set("ru")
    try:
        rich = unified_merchant_bill_rich(summary, preview)
    finally:
        _language.reset(token)
    assert "Итого к оплате</b></td><td><b>150.00 ₽" in rich
    assert "515.00 ₽" in rich
    assert "210.00 ₽" in rich
    assert "⚠️" in rich


def test_inconsistent_financial_totals_cannot_be_rendered():
    summary = deepcopy(SUMMARY)
    summary["total_due_rub"] = "851"
    with pytest.raises(ValueError, match="Inconsistent"):
        unified_merchant_bill_rich(summary, PREVIEW)
