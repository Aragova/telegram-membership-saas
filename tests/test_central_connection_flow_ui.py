"""Approved RU/EN central connection and account billing cards."""
from types import SimpleNamespace

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.bots.deletion_service import RetirementBlockers
from telegram_membership_saas.modules.owner_setup.account_bill_ui import (
    account_bill_classic,
    account_bill_rich,
)
from telegram_membership_saas.modules.owner_setup.central_connection_flow_ui import (
    disconnect_card,
    obligations_card,
    rename_input_card,
    token_input_card,
)


@pytest.mark.parametrize("lang,token_title,rename_title,input_hint", [
    ("ru", "ОБНОВЛЕНИЕ ТОКЕНА", "НАЗВАНИЕ ПОДКЛЮЧЕНИЯ",
     "ОТПРАВЬТЕ ТОКЕН ПРЯМО СЮДА"),
    ("en", "UPDATE BOT TOKEN", "CONNECTION NAME",
     "SEND THE TOKEN RIGHT HERE"),
])
def test_token_and_rename_are_bilingual_rich_and_classic(
    lang, token_title, rename_title, input_hint,
):
    ctx = _language.set(lang)
    try:
        rich = token_input_card(username="test<&bot", rich=True)
        classic = token_input_card(username="test<&bot", rich=False)
        rename_rich = rename_input_card(username="test<&bot", rich=True)
        rename_classic = rename_input_card(username="test<&bot", rich=False)
    finally:
        _language.reset(ctx)
    for text in (rich, classic):
        assert token_title in text and input_hint in text
        assert "BotFather" in text and "API Token" in text
        assert "@test&lt;&amp;bot" in text
        assert "@test<&bot" not in text
    for text in (rename_rich, rename_classic):
        assert rename_title in text
        assert "@test&lt;&amp;bot" in text
        assert "120" in text
    assert "<h3>" in rich and "<h3>" in rename_rich
    assert "<h3>" not in classic and "<h3>" not in rename_classic


@pytest.mark.parametrize("lang,heading", [
    ("ru", "ОТКЛЮЧЕНИЕ БОТА"),
    ("en", "DISCONNECT BOT"),
])
def test_disconnect_only_claims_safety_when_all_blockers_are_gone(lang, heading):
    ctx = _language.set(lang)
    try:
        safe = disconnect_card(
            username="vimitest21bot", blockers=RetirementBlockers(), rich=True,
        )
        blocked = disconnect_card(
            username="vimitest21bot",
            blockers=RetirementBlockers(
                requests=2, subscriptions=3, file_orders=1,
                stars_checkouts=1, native_paid_links=1,
            ),
            rich=True,
        )
    finally:
        _language.reset(ctx)
    assert heading in safe and heading in blocked
    assert "@vimitest21bot" in safe
    assert "✅" in safe
    assert "⛔" in blocked and "⚠️" in blocked
    assert "📂" in blocked and "⭐" in blocked and "📣" in blocked
    assert "API Token" not in blocked


@pytest.mark.parametrize("lang,title", [
    ("ru", "ПОКУПАТЕЛИ И ДОСТУПЫ"),
    ("en", "CUSTOMERS AND ACCESS"),
])
def test_obligation_summary_has_plain_language_and_no_internal_ids(lang, title):
    ctx = _language.set(lang)
    try:
        blockers = RetirementBlockers(
            requests=2, subscriptions=3, grants=1, access=1,
            file_orders=1, stars_checkouts=1, native_paid_links=1,
        )
        rich = obligations_card(
            username="vimitest21bot", blockers=blockers, rich=True,
            section="overview", page=0,
        )
        classic = obligations_card(
            username="vimitest21bot", blockers=blockers, rich=False,
            section="overview", page=0,
        )
    finally:
        _language.reset(ctx)
    for text in (rich, classic):
        assert title in text
        assert "2" in text and "3" in text
        assert "Stars" in text and "⭐" in text
        assert "file_orders" not in text
        assert "access_actions" not in text
        assert "UUID" not in text


@pytest.mark.parametrize("lang,title", [
    ("ru", "СЧЁТ АККАУНТА"),
    ("en", "ACCOUNT BILL"),
])
def test_no_debt_account_bill_matches_approved_state(lang, title):
    ctx = _language.set(lang)
    try:
        statement = SimpleNamespace(unpaid=0, total=0, outstanding_rub="0")
        billing = {"overdue": 0, "overdue_rub": "0", "next": None}
        rich = account_bill_rich(statement, billing)
        classic = account_bill_classic(statement, billing)
    finally:
        _language.reset(ctx)
    for text in (rich, classic):
        assert title in text
        assert "0 ₽" in text
        assert "✅" in text
        assert "📊" in text
        assert "uuid" not in text.lower()
    assert "<h3>" in rich and "<table" in rich
    assert "<h3>" not in classic and "<table" not in classic


@pytest.mark.parametrize("lang,title", [
    ("ru", "ЕСТЬ НЕОПЛАЧЕННЫЕ СЧЕТА"),
    ("en", "UNPAID INVOICES"),
])
def test_account_debt_shows_real_overdue_and_nearest_issued_bill(lang, title):
    ctx = _language.set(lang)
    try:
        statement = SimpleNamespace(
            unpaid=2, total=4, outstanding_rub="300.00",
        )
        billing = {
            "overdue": 1, "overdue_rub": "150",
            "next": {
                "invoice_id": "internal-id-not-for-ui",
                "username": "vimitest21bot",
                "due_at": "2026-12-01T12:00:00+03:00",
                "remaining_rub": "150", "same_day_count": 2,
            },
        }
        rich = account_bill_rich(statement, billing)
        classic = account_bill_classic(statement, billing)
    finally:
        _language.reset(ctx)
    for text in (rich, classic):
        assert title in text and "@vimitest21bot" in text
        assert "300 ₽" in text and "150 ₽" in text
        assert "01.12.2026" in text
        assert "internal-id-not-for-ui" not in text
        assert "2" in text and "1" in text


def test_account_bill_escapes_bot_username_and_does_not_invent_future_date():
    statement = SimpleNamespace(unpaid=1, total=1, outstanding_rub="150")
    home = {"overdue": 1, "overdue_rub": "150", "next": None}
    rich = account_bill_rich(statement, home)
    assert "БЛИЖАЙШИЙ ПЛАТЁЖ" not in rich
    home["next"] = {
        "username": "a<&b", "due_at": "2026-12-01",
        "remaining_rub": "150", "same_day_count": 1,
    }
    rich = account_bill_rich(statement, home)
    assert "@a&lt;&amp;b" in rich
    assert "@a<&b" not in rich
