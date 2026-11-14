"""Regression: ViMi Pay merchant invoice screens stay minimal and navigable.

No real payment attempts, no invoice settlement, no debug/test buttons.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.platform_saas_payment_service import (
    MerchantSaasStatements,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _callback(data: str, *, message_id: int | None = 88):
    msg = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=777),
    )
    if message_id is not None:
        msg.message_id = message_id
    return SimpleNamespace(
        data="own:" + data,
        from_user=SimpleNamespace(
            id=777, username="merchant", first_name="Merchant",
        ),
        message=msg,
        answer=AsyncMock(),
    )


def _handler(project):
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    return next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.asyncio
async def test_connected_invoice_menu_has_only_bill_and_main(monkeypatch, lang):
    invoice_id, project = uuid4(), uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    report = MerchantSaasStatements(
        total=1, unpaid=1, outstanding_rub=150,
        configured=True, instructions="PRIVATE BANK DETAILS", page=0,
        invoices=({
            "id": str(invoice_id), "kind": "prepayment",
            "status": "unpaid", "amount_rub": "150.00",
            "due_at": "2026-11-16T00:00:00+00:00",
        },),
    )
    statements = AsyncMock(return_value=report)
    monkeypatch.setattr(ui, "merchant_saas_statements", statements)
    unused = AsyncMock(side_effect=AssertionError(
        "Invoice index must not query trial or commission analytics",
    ))
    monkeypatch.setattr(ui, "merchant_saas_trial_status", unused)
    monkeypatch.setattr(ui, "saas_price_profile", unused)
    bot = MagicMock()
    bot.id = 789
    bot.send_message = AsyncMock()
    token = _language.set(lang)
    try:
        query = _callback("saasinvoices:0")
        await _handler(project)(query, bot)
        query.answer.assert_awaited_once()
    finally:
        _language.reset(token)
    statements.assert_awaited_once()
    unused.assert_not_awaited()
    body = bot.send_message.await_args.args[1]
    buttons = [
        b for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard for b in row
    ]
    assert len(buttons) == 2
    assert buttons[0].callback_data == (
        "own:saasbill:" + encode_uuid(invoice_id) + ":0"
    )
    assert buttons[1].callback_data == "own:dashboard"
    assert ("Главное меню" if lang == "ru" else "Main menu") in buttons[1].text
    assert "150.00 ₽" in buttons[0].text
    assert str(invoice_id) not in body
    assert str(invoice_id)[:8] not in buttons[0].text
    assert "PRIVATE BANK DETAILS" not in body
    assert "Продажи" not in body
    assert "Настройки" not in body
    assert "Обновить" not in str(buttons)
    assert not any(b.callback_data.startswith("own:spm:") for b in buttons)


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.asyncio
async def test_payment_selector_edits_one_message_and_preserves_rails(
    monkeypatch, lang,
):
    invoice, method, project = uuid4(), uuid4(), uuid4()
    token = encode_uuid(invoice)
    method_token = encode_uuid(method)
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "saas_v2_invoice_balance", AsyncMock(
        return_value={"remaining_rub": "150.00"},
    ))
    monkeypatch.setattr(ui, "saas_catalog_for_invoice", AsyncMock(
        return_value=({"id": str(method), "currency": "RUB", "name": "RUB"},),
    ))
    payment = AsyncMock(return_value={
        "id": str(uuid4()), "currency": "RUB", "amount": "150.00",
        "rub_per_unit": "1", "instructions": "Bank text",
    })
    monkeypatch.setattr(ui, "saas_v3_create_attempt", payment)
    bot = MagicMock()
    bot.id = 909
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    handler = _handler(project)
    language = _language.set(lang)
    try:
        q1 = _callback(f"spm:{token}:RUB:0")
        await handler(q1, bot)
        q1.answer.assert_awaited_once()
        first = bot.edit_message_text.await_args
        assert "150.00 ₽" in first.args[0]
        assert "Оплата по реквизитам" in str(first.kwargs["reply_markup"]) if (
            lang == "ru"
        ) else "Payment instructions" in str(first.kwargs["reply_markup"])
        assert "₽ " in str(first.kwargs["reply_markup"])
        assert first.kwargs["message_id"] == 88
        assert bot.send_message.await_count == 0

        q2 = _callback(f"spv:{token}:{method_token}:RUB:0")
        await handler(q2, bot)
        assert bot.edit_message_text.await_count == 2
        second = bot.edit_message_text.await_args
        assert second.kwargs["message_id"] == 88
        assert "150.00 ₽" in str(second.kwargs["reply_markup"])
        assert bot.send_message.await_count == 0

        q3 = _callback(f"spc:{token}:{method_token}:RUB:F:0")
        await handler(q3, bot)
        payment.assert_awaited_once()
        assert payment.await_args.kwargs["invoice_id"] == invoice
        assert payment.await_args.kwargs["method_id"] == method
        assert payment.await_args.kwargs["rub"] == 150
        bot.send_message.assert_awaited_once()
        instruction = bot.send_message.await_args.args[1]
        assert "Bank text" in instruction
        assert "Курс ЦБ: 1" not in instruction
    finally:
        _language.reset(language)


@pytest.mark.asyncio
async def test_no_invoice_menu_displays_only_main_button(monkeypatch):
    project = uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "merchant_saas_statements", AsyncMock(
        return_value=MerchantSaasStatements(
            total=0, unpaid=0, outstanding_rub=0,
            configured=True, instructions=None, invoices=(), page=0,
        ),
    ))
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.id = 123
    await _handler(project)(_callback("saasinvoices:0"), bot)
    rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 1
    assert len(rows[0]) == 1
    assert rows[0][0].callback_data == "own:dashboard"
