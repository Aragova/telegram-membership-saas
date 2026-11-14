"""Owner UI regression: a single safe void action in the existing invoice card."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _callback(data):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(
            id=7777, username="operator", first_name="Operator",
        ),
        message=SimpleNamespace(
            message_id=101, chat=SimpleNamespace(
                id=7777, type="private",
            ),
        ),
        answer=AsyncMock(),
    )


def _handler():
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=Settings(platform_owner_telegram_id=7777),
    )
    return next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "platform_callback"
    )


@pytest.mark.asyncio
async def test_test_invoice_void_requires_confirm_and_uses_same_invoice_card(
    monkeypatch,
):
    invoice_id, owner = uuid4(), uuid4()
    item = {
        "id": str(invoice_id), "owner": str(owner),
        "merchant": str(uuid4()), "kind": "prepayment",
        "status": "issued", "total_rub": "150.00",
        "due_at": "2026-11-16T00:00:00+00:00",
    }
    monkeypatch.setattr(
        ui, "platform_saas_invoice_report",
        AsyncMock(return_value=SimpleNamespace(
            invoices=(item,), enabled=False,
        )),
    )
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        ui, "saas_invoice_currency_snapshot",
        AsyncMock(return_value={"locked": False}),
    )
    monkeypatch.setattr(
        ui, "saas_invoice_credit_history",
        AsyncMock(return_value={"total_credited_rub": "0", "entries": []}),
    )
    render = AsyncMock()
    card = AsyncMock()
    monkeypatch.setattr(ui, "_render_platform_screen", render)
    monkeypatch.setattr(ui, "send_owner_rich", card)
    void = AsyncMock(return_value={"status": "void"})
    monkeypatch.setattr(ui, "void_saas_test_prepayment", void)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    callback = _handler()
    token = encode_uuid(invoice_id)

    await callback(_callback(f"plat:billcheck:{token}:0"), bot)
    buttons = [
        btn for row in card.await_args.kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert len([
        btn for btn in buttons if "Аннулировать тестовый счёт" in btn.text
    ]) == 1
    assert any(
        btn.callback_data == f"plat:billvoidask:{token}:0"
        for btn in buttons
    )
    void.assert_not_awaited()

    await callback(_callback(f"plat:billvoidask:{token}:0"), bot)
    assert "150.00" in render.await_args.kwargs["text"]
    assert "История сохранится" in render.await_args.kwargs["text"]
    confirm = render.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert confirm.callback_data.startswith(f"plat:billvoidok:{token}:0:")
    void.assert_not_awaited()

    await callback(_callback(confirm.callback_data), bot)
    void.assert_awaited_once()
    assert void.await_args.kwargs["invoice_id"] == invoice_id
    assert void.await_args.kwargs["expected_owner_id"] == owner
    assert void.await_args.kwargs["expected_amount"] == Decimal("150.00")
    assert "Тестовый счёт аннулирован" in render.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_live_billing_never_offers_void_and_stale_confirmation_is_rejected(
    monkeypatch,
):
    invoice_id = uuid4()
    item = {
        "id": str(invoice_id), "owner": str(uuid4()),
        "merchant": str(uuid4()), "kind": "prepayment",
        "status": "issued", "total_rub": "150.00",
        "due_at": "2026-11-16T00:00:00+00:00",
    }
    monkeypatch.setattr(
        ui, "platform_saas_invoice_report",
        AsyncMock(return_value=SimpleNamespace(
            invoices=(item,), enabled=True,
        )),
    )
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        ui, "saas_invoice_currency_snapshot",
        AsyncMock(return_value={"locked": False}),
    )
    monkeypatch.setattr(
        ui, "saas_invoice_credit_history",
        AsyncMock(return_value={"total_credited_rub": "0", "entries": []}),
    )
    card = AsyncMock()
    monkeypatch.setattr(ui, "send_owner_rich", card)
    void = AsyncMock()
    monkeypatch.setattr(ui, "void_saas_test_prepayment", void)
    bot = MagicMock()
    handler = _handler()
    token = encode_uuid(invoice_id)

    await handler(_callback(f"plat:billcheck:{token}:0"), bot)
    buttons = [
        btn for row in card.await_args.kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert all("billvoidask" not in btn.callback_data for btn in buttons)
    forged = _callback(f"plat:billvoidok:{token}:0:fake")
    await handler(forged, bot)
    void.assert_not_awaited()
    forged.answer.assert_awaited_once()
    assert forged.answer.await_args.kwargs.get("show_alert") is True
