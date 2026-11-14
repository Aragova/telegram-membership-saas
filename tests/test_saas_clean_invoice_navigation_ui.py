"""Regression: ViMi Pay merchant invoice screens stay minimal and navigable.

No real payment attempts, no invoice settlement, no debug/test buttons.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
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
async def test_unified_bill_displays_all_revenue_types_and_one_pay(monkeypatch, lang):
    invoice_id, project = uuid4(), uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    summary = AsyncMock(return_value={
        "month": "2026-11-01", "project_id": str(project),
        "turnover": [
            {"kind": "subscription", "currency": "RUB",
             "sold": 20, "gross": "10000.00"},
            {"kind": "file", "currency": "USD",
             "sold": 10, "gross": "100.00"},
        ],
        "service_due_rub": "150", "commission_due_rub": "700",
        "total_due_rub": "850", "unpaid_count": 1,
        "first_invoice_id": str(invoice_id),
    })
    preview = AsyncMock(return_value={
        "rails": [
            {"kind": "subscription", "commission_rub": "500"},
            {"kind": "file_manual", "commission_rub": "200"},
        ],
        "unrated_billable": 0, "uncaptured_crypto": 0,
    })
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", summary)
    monkeypatch.setattr(ui, "merchant_saas_unified_monthly_preview", preview)
    old_list = AsyncMock(side_effect=AssertionError(
        "Unified bill must not fetch individual invoice buttons",
    ))
    monkeypatch.setattr(ui, "merchant_saas_statements", old_list)
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
    summary.assert_awaited_once()
    preview.assert_awaited_once()
    old_list.assert_not_awaited()
    assert preview.await_args.kwargs["project_id"] == project
    body = bot.send_message.await_args.args[1]
    buttons = [
        b for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard for b in row
    ]
    assert len(buttons) == 2
    assert buttons[0].callback_data == (
        "own:spm:" + encode_uuid(invoice_id) + ":RUB:0"
    )
    assert buttons[1].callback_data == "own:dashboard"
    assert ("Главное меню" if lang == "ru" else "Main menu") in buttons[1].text
    assert "150.00 ₽" in body
    assert "850.00 ₽" in body
    assert "10 000" not in body or "10000" in body
    assert "100 USD" in body
    assert ("Подписки" if lang == "ru" else "Subscriptions") in body
    assert ("Файлы" if lang == "ru" else "Files") in body
    assert str(invoice_id) not in body
    assert "Настройки" not in body
    assert "Обновить" not in str(buttons)


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
async def test_zero_due_bill_displays_sections_but_no_pay(monkeypatch):
    project = uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", AsyncMock(
        return_value={
            "month": "2026-11-01", "project_id": str(project),
            "turnover": [], "service_due_rub": "0",
            "commission_due_rub": "0", "total_due_rub": "0",
            "unpaid_count": 0, "first_invoice_id": None,
        },
    ))
    monkeypatch.setattr(ui, "merchant_saas_unified_monthly_preview", AsyncMock(
        return_value={"rails": [], "unrated_billable": 0,
                      "uncaptured_crypto": 0},
    ))
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.id = 123
    await _handler(project)(_callback("saasinvoices:0"), bot)
    rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 1
    assert len(rows[0]) == 1
    assert rows[0][0].callback_data == "own:dashboard"
    body = bot.send_message.await_args.args[1]
    assert "Подписки" in body and "Файлы" in body
    assert "0.00 ₽" in body


@pytest.mark.asyncio
async def test_several_debts_open_one_internal_payment_choice(monkeypatch):
    project = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", AsyncMock(
        return_value={
            "month": "2026-11-01", "project_id": str(project),
            "turnover": [], "service_due_rub": "150",
            "commission_due_rub": "20", "total_due_rub": "170",
            "unpaid_count": 2, "first_invoice_id": str(uuid4()),
        },
    ))
    monkeypatch.setattr(ui, "merchant_saas_unified_monthly_preview", AsyncMock(
        return_value={"rails": [], "unrated_billable": 0,
                      "uncaptured_crypto": 0},
    ))
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await _handler(project)(_callback("saasinvoices:0"), bot)
    rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 2
    assert rows[0][0].callback_data == "own:saaspaychoices:0"
    assert rows[1][0].callback_data == "own:dashboard"
