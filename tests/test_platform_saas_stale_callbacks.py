"""Stale financial callbacks never confirm a different quote or receipt."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_dashboard_handlers as platform
from telegram_membership_saas.modules.owner_setup import handlers as merchant
from telegram_membership_saas.modules.platform_saas_invoice_ui import (
    quote_fingerprint,
    receipt_fingerprint,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


async def test_stale_merchant_quote_button_never_locks_new_rate(monkeypatch):
    invoice = uuid4()
    actor = uuid4()
    old = {
        "invoice_id": str(invoice), "currency": "EUR",
        "quote_day": "2026-11-11", "source_day": "2026-11-10",
        "rub_per_unit": "90", "amount": "1.67",
        "total_rub": "150", "quote_ready": True,
    }
    changed = dict(old, quote_day="2026-11-12", rub_per_unit="100",
                   amount="1.50")
    mock_preview = AsyncMock(return_value=changed)
    mock_lock = AsyncMock()
    monkeypatch.setattr(
        merchant, "ensure_telegram_actor", AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(merchant, "preview_saas_invoice_currency", mock_preview)
    monkeypatch.setattr(merchant, "lock_saas_invoice_currency_expected", mock_lock)
    monkeypatch.setattr(merchant, "get_selected_project", AsyncMock(
        side_effect=AssertionError("No current project required"),
    ))
    router = merchant.create_owner_setup_router(MagicMock(), fixed_project_id=None)
    callback_handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    callback_data = (
        "own:sl:" + encode_uuid(invoice) + ":0:EUR:20261111:"
        + quote_fingerprint(old)
    )
    bot = MagicMock()
    bot.id = 123456
    bot.send_message = AsyncMock()
    query = SimpleNamespace(
        data=callback_data,
        from_user=SimpleNamespace(
            id=300001, username="merchant", first_name="Merchant",
        ),
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )
    language = _language.set("ru")
    try:
        await callback_handler(query, bot)
    finally:
        _language.reset(language)
    mock_preview.assert_awaited_once()
    mock_lock.assert_not_awaited()
    assert "Условия расчёта изменились" in bot.send_message.await_args.args[1]
    query.answer.assert_awaited_once()


@pytest.mark.parametrize("changed", [
    {"locked": True, "currency": "EUR", "amount": "1.67",
     "rub_per_unit": "90", "quote_day": "2026-11-11"},
    {"locked": False},
])
async def test_platform_receipt_callback_rejects_changed_or_paid_card(
    monkeypatch, changed,
):
    invoice_id, actor_id = uuid4(), uuid4()
    record = {
        "id": str(invoice_id), "kind": "commission",
        "status": "unpaid", "total_rub": "150",
        "due_at": "2026-11-20T12:00:00Z",
    }
    original = {"locked": False}
    fingerprint = receipt_fingerprint(invoice_id, record, original)
    now = dict(record)
    if not changed.get("locked"):
        now["status"] = "paid"
    report = SimpleNamespace(invoices=[now])
    monkeypatch.setattr(
        platform, "is_configured_platform_owner", lambda *_args: True,
    )
    monkeypatch.setattr(
        platform, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    monkeypatch.setattr(
        platform, "platform_saas_invoice_report",
        AsyncMock(return_value=report),
    )
    monkeypatch.setattr(
        platform, "saas_invoice_currency_snapshot",
        AsyncMock(return_value=changed),
    )
    confirm_rub, confirm_foreign = AsyncMock(), AsyncMock()
    monkeypatch.setattr(platform, "confirm_saas_receipt", confirm_rub)
    monkeypatch.setattr(
        platform, "confirm_saas_currency_receipt", confirm_foreign,
    )
    render = AsyncMock()
    monkeypatch.setattr(platform, "_render_platform_screen", render)
    router = platform.create_platform_dashboard_router(
        MagicMock(), settings=MagicMock(),
    )
    callback_handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "platform_callback"
    )
    user = SimpleNamespace(
        id=400001, username="operator", first_name="Operator",
    )
    query = SimpleNamespace(
        data="plat:billcommit:" + encode_uuid(invoice_id) + ":0:" + fingerprint,
        from_user=user,
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=user.id),
            message_id=44,
        ),
        answer=AsyncMock(),
    )
    bot = MagicMock()
    lang = _language.set("ru")
    try:
        await callback_handler(query, bot)
    finally:
        _language.reset(lang)
    confirm_rub.assert_not_awaited()
    confirm_foreign.assert_not_awaited()
    render.assert_awaited_once()
    assert "Условия счёта изменились" in render.await_args.kwargs["text"]
    query.answer.assert_awaited_once()
