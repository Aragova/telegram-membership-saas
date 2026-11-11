"""Issued ViMi Pay bills: bilingual Rich, classic and audited credit rendering."""
from decimal import Decimal

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.platform_saas_invoice_ui import (
    issued_invoice_classic,
    issued_invoice_rich,
    issued_quote_classic,
    issued_quote_rich,
)

INVOICE = {
    "id": "be097af9-cb55-46b9-91be-101b52050042",
    "kind": "commission",
    "status": "unpaid",
    "due_at": "2026-11-20T12:00:00+00:00",
    "amount_rub": "150.00",
}
SNAPSHOT = {
    "locked": True,
    "currency": "EUR",
    "amount": "1.67",
    "rub_per_unit": "89.6603",
    "quote_day": "2026-11-11",
    "source": "CBR",
    "source_day": "2026-11-10",
}
CREDITS = {
    "total_credited_rub": "25.00",
    "credit_count": 1,
    "entries": [{
        "reason": "refund", "reference": "bank:<refund>&001",
        "credit_rub": "25.00", "at": "2026-11-11",
    }],
    "application_status": "pending_future_statement",
}


@pytest.mark.parametrize("lang,word", [
    ("ru", "Выставленный счёт"),
    ("en", "Issued invoice"),
])
def test_issued_invoice_rich_and_classic_are_bilingual_and_escape_sources(lang, word):
    token = _language.set(lang)
    try:
        rich = issued_invoice_rich(INVOICE, SNAPSHOT, CREDITS)
        classic = issued_invoice_classic(INVOICE, SNAPSHOT, CREDITS)
    finally:
        _language.reset(token)
    assert word in rich and word in classic
    assert "<table bordered compact>" in rich
    assert "<table" not in classic
    for data in (rich, classic):
        assert "1.67 EUR" in data
        assert "89.6603 ₽" in data
        assert "2026-11-10" in data
        assert "25.00 ₽" in data
        assert "bank:&lt;refund&gt;&amp;001" in data
        assert "<refund>" not in data
        assert "150.00 ₽" in data


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_unlocked_invoice_is_not_presented_as_payable_in_foreign_currency(lang):
    token = _language.set(lang)
    try:
        rich = issued_invoice_rich(
            INVOICE, {"locked": False},
            {"total_credited_rub": Decimal(0), "entries": []},
        )
    finally:
        _language.reset(token)
    assert "<table bordered compact>" in rich
    assert "1.67 EUR" not in rich
    assert "89.6603" not in rich


@pytest.mark.parametrize("lang,freeze_notice", [
    ("ru", "изменить нельзя"),
    ("en", "cannot be changed"),
])
def test_issued_quote_is_preliminary_until_confirmed(lang, freeze_notice):
    quote = {
        "invoice_id": INVOICE["id"],
        "currency": "EUR", "total_rub": "150.00",
        "quote_day": "2026-11-11", "source_day": "2026-11-10",
        "rub_per_unit": "89.6603", "amount": "1.67",
        "quote_ready": True,
    }
    token = _language.set(lang)
    try:
        rich = issued_quote_rich(quote)
        classic = issued_quote_classic(quote)
    finally:
        _language.reset(token)
    assert freeze_notice in rich
    assert "<table bordered compact>" in rich
    assert "<table" not in classic
    assert "1.67 EUR" in rich and "1.67 EUR" in classic
    assert "2026-11-10" in rich
    assert "150.00 ₽" in rich


def test_missing_quote_does_not_display_fabricated_amount():
    quote = {
        "invoice_id": INVOICE["id"],
        "currency": "UAH", "total_rub": "150.00",
        "quote_day": "2026-11-11", "source_day": None,
        "rub_per_unit": None, "amount": None,
        "quote_ready": False,
    }
    token = _language.set("ru")
    try:
        rich, classic = issued_quote_rich(quote), issued_quote_classic(quote)
    finally:
        _language.reset(token)
    assert "Фиксация недоступна" in rich
    assert "Подтверждение недоступно" in classic
    assert "None" not in rich + classic
