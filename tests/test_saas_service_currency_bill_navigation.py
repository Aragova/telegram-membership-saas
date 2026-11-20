"""The tariff currency lives in Bill, never Settings. No debt repricing."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.platform_saas_bill_ui import (
    unified_merchant_bill_classic,
    unified_merchant_bill_rich,
)

BILL = {
    "invoice_id": str(uuid4()), "turnover": [],
    "first_period": True,
    "service_rub": "150", "subscription_rub": "0", "file_rub": "0",
    "total_rub": "150", "paid_rub": "150", "credit_rub": "0",
    "total_due_rub": "0", "hold_state": "clear",
    "period_start": "2026-11-16T17:53:00+03:00",
    "period_end": "2026-12-16T17:53:00+03:00",
}


def _callback(action):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(id=777, username=None, first_name="Admin"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=777)),
        answer=AsyncMock(),
    )


@pytest.mark.parametrize("lang", ("ru", "en"))
@pytest.mark.parametrize("state", ("issued", "paid", "unissued"))
def test_price_hint_on_all_bill_states_and_ru_en(lang, state):
    bill = deepcopy(BILL)
    if state == "issued":
        bill["paid_rub"] = "0"
        bill["total_due_rub"] = "150"
    if state == "unissued":
        bill["invoice_id"] = None
    token = _language.set(lang)
    try:
        for renderer in (unified_merchant_bill_rich, unified_merchant_bill_classic):
            screen = renderer(bill, future_profile_currency="USD")
            assert "USD" in screen and "$5" in screen
            assert ("Новые счета" if lang == "ru"
                    else "Future bills") in screen
            if renderer is unified_merchant_bill_rich:
                assert "<table bordered compact>" in screen
                assert screen.count("<table bordered compact>") >= 1
            if state != "unissued":
                assert "150.00 ₽" in screen  # Original invoice unchanged
    finally:
        _language.reset(token)


@pytest.mark.parametrize("lang", ("ru", "en"))
def test_paid_bill_with_network_hold_names_other_unpaid_invoice(lang):
    bill = deepcopy(BILL)
    bill["hold_state"] = "held"
    token = _language.set(lang)
    try:
        screen = unified_merchant_bill_rich(bill, future_profile_currency="RUB")
    finally:
        _language.reset(token)
    assert ("Этот счёт оплачен" if lang == "ru" else
            "This invoice is paid") in screen
    assert ("другого неоплаченного счёта" if lang == "ru" else
            "another ViMi Pay invoice") in screen


@pytest.mark.parametrize("project_scoped", (True, False))
@pytest.mark.parametrize("lang", ("ru", "en"))
async def test_currency_picker_is_bill_only_and_uses_existing_profile_rpc(
    monkeypatch, project_scoped, lang,
):
    project = uuid4() if project_scoped else None
    actor_id = uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    calls = []
    current = {"currency": "RUB", "monthly_amount": "150.00"}

    async def price_profile(_factory, *, actor_id, project_id, currency=None):
        assert actor_id is not None
        assert project_id == project
        calls.append(currency)
        if currency is not None:
            assert currency in {"RUB", "USD"}
            current["currency"] = currency
            current["monthly_amount"] = "5.00" if currency == "USD" else "150.00"
        return current.copy()

    monkeypatch.setattr(ui, "saas_price_profile", price_profile)
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    handler = next(h.callback for h in router.callback_query.handlers
                   if h.callback.__name__ == "owner_callback")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    locale = _language.set(lang)
    try:
        picker = _callback("saasprofile")
        await handler(picker, bot)
        first = bot.send_message.await_args
        assert "150 ₽" in first.args[1]
        assert (
            ("Только будущие счета" if lang == "ru" else
             "This bot's future invoices") if project_scoped
            else ("Валюта по умолчанию" if lang == "ru" else
                  "Default currency")
        ) in first.args[1]
        actions = [
            b.callback_data for row in first.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert actions == (
            ["own:saasprice:RUB", "own:saasprice:USD", "own:saasinvoices:0"]
        )
        switched = _callback("saasprice:USD")
        await handler(switched, bot)
        assert "$5" in bot.send_message.await_args.args[1]
        assert calls == [None, "USD", None]
    finally:
        _language.reset(locale)


def test_settings_no_longer_contains_financial_menu_item():
    import inspect

    source = inspect.getsource(ui.create_owner_setup_router)
    settings_section = source.split("    async def show_settings(", 1)[1].split(
        "    async def show_saas_service_profile(", 1,
    )[0]
    assert "saasprofile" not in settings_section
    assert "Валюта обслуживания" in source
    assert "saasinvoices:0" in source


@pytest.mark.parametrize("lang", ("ru", "en"))
@pytest.mark.parametrize("connected", (True, False))
async def test_unpaid_invoice_preserves_tariff_choice_and_explains_block(
    monkeypatch, lang, connected,
):
    """Existing database guard blocks the switch, but Telegram explains why."""
    project = uuid4() if connected else None
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))

    class UnpaidInvoice(Exception):
        sqlstate = "23514"

    save = AsyncMock(side_effect=DBAPIError(
        "SELECT public.platform_saas_price_profile_set(:currency)", {},
        UnpaidInvoice("Settle outstanding service invoice before changing profile"),
    ))
    monkeypatch.setattr(ui, "saas_price_profile", save)
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    token = _language.set(lang)
    try:
        callback = _callback("saasprice:USD")
        await handler(callback, bot)
        assert callback.answer.await_count == 1
        options = callback.answer.await_args.kwargs
        assert options["show_alert"] is True
        message = callback.answer.await_args.args[0]
        assert ("Сначала погасите" if lang == "ru"
                else "Settle outstanding") in message
        bot.send_message.assert_not_awaited()
        save.assert_awaited_once()
    finally:
        _language.reset(token)
