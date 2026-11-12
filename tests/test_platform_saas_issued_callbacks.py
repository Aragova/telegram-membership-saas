"""Issued invoice card remains accessible from central historical account billing."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.platform_saas_invoice_ui import quote_fingerprint
from telegram_membership_saas.modules.platform_saas_payment_service import (
    MerchantSaasStatements,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_issued_bill_card_works_without_selected_project(monkeypatch, language):
    invoice_id = uuid4()
    actor_id = uuid4()
    user = SimpleNamespace(id=100102, username="history", first_name="Merchant")
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    get_project = AsyncMock(side_effect=AssertionError(
        "Historical invoice detail should not need a connected bot"
    ))
    monkeypatch.setattr(ui, "get_selected_project", get_project)
    report = MerchantSaasStatements(
        total=1, unpaid=1, outstanding_rub=Decimal("150"),
        configured=False, instructions=None, page=0,
        invoices=({
            "id": str(invoice_id), "kind": "prepayment",
            "status": "unpaid", "amount_rub": "150",
            "due_at": "2026-11-18T00:00:00Z",
        },),
    )
    get_statements = AsyncMock(return_value=report)
    monkeypatch.setattr(ui, "merchant_saas_statements", get_statements)
    snapshot = AsyncMock(return_value={"locked": False})
    credit = AsyncMock(return_value={
        "total_credited_rub": "0", "entries": [], "credit_count": 0,
    })
    monkeypatch.setattr(ui, "saas_invoice_currency_snapshot", snapshot)
    monkeypatch.setattr(ui, "saas_invoice_credit_history", credit)
    balance = AsyncMock(return_value={"remaining_rub": "150.00"})
    monkeypatch.setattr(ui, "saas_v2_invoice_balance", balance)
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=None)
    callback_handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    bot = MagicMock()
    bot.id = 123456
    bot.send_message = AsyncMock()
    callback = SimpleNamespace(
        data="own:saasbill:" + encode_uuid(invoice_id) + ":0",
        from_user=user,
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )
    token = _language.set(language)
    try:
        await callback_handler(callback, bot)
    finally:
        _language.reset(token)

    get_project.assert_not_awaited()
    snapshot.assert_awaited_once()
    credit.assert_awaited_once()
    balance.assert_awaited_once()
    assert balance.await_args.kwargs["project_id"] is None
    get_statements.assert_awaited_once()
    assert get_statements.await_args.kwargs["project_id"] is None
    assert get_statements.await_args.kwargs["actor_user_id"] == actor_id
    bot.send_message.assert_awaited_once()
    body = bot.send_message.await_args.args[1]
    assert str(invoice_id) in body
    assert "150.00 ₽" in body
    buttons = [
        b.callback_data
        for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for b in row
    ]
    assert "own:saasbillchoose:" + encode_uuid(invoice_id) + ":0" in buttons
    assert "own:saasinvoices:0" in buttons
    callback.answer.assert_awaited_once()


async def test_currency_preview_is_read_only_until_confirm(monkeypatch):
    actor_id, invoice_id = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(
        side_effect=AssertionError("Cannot depend on selected project"),
    ))
    preview = AsyncMock(return_value={
        "invoice_id": str(invoice_id), "currency": "EUR",
        "total_rub": "150", "quote_day": "2026-11-11",
        "source_day": "2026-11-10",
        "rub_per_unit": "90", "amount": "1.67",
        "quote_ready": True, "preview_only": True,
    })
    lock = AsyncMock(side_effect=AssertionError("Preview must not lock funds"))
    monkeypatch.setattr(ui, "preview_saas_invoice_currency", preview)
    monkeypatch.setattr(ui, "lock_saas_invoice_currency_expected", lock)
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=None)
    callback_handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    bot = MagicMock()
    bot.id = 123456
    bot.send_message = AsyncMock()
    callback = SimpleNamespace(
        data="own:saasbillquote:" + encode_uuid(invoice_id) + ":0:EUR",
        from_user=SimpleNamespace(id=100103, username=None, first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )
    token = _language.set("ru")
    try:
        await callback_handler(callback, bot)
    finally:
        _language.reset(token)
    preview.assert_awaited_once()
    lock.assert_not_awaited()
    assert "1.67 EUR" in bot.send_message.await_args.args[1]
    buttons = [
        b.callback_data
        for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for b in row
    ]
    expected = (
        "own:sl:" + encode_uuid(invoice_id) + ":0:EUR:20261111:"
        + quote_fingerprint(preview.return_value)
    )
    assert expected in buttons
    assert len(expected.encode("utf-8")) <= 64
