"""Per-bot ViMi Pay bill: two actions, full payment only, RU/EN."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _callback(data: str):
    return SimpleNamespace(
        data="own:" + data,
        from_user=SimpleNamespace(id=777, username="merchant", first_name="Admin"),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=777), message_id=88,
        ),
        answer=AsyncMock(),
    )


def _handler(project):
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    return next(h.callback for h in router.callback_query.handlers
                if h.callback.__name__ == "owner_callback")


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_bot_invoice_has_single_pay_and_home(monkeypatch, lang):
    project, invoice = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    summary = AsyncMock(return_value={
        "bot_id": str(uuid4()), "project_id": str(project),
        "invoice_id": str(invoice), "period_start": "2026-12-16T00:00:00+03:00",
        "period_end": "2027-01-16T00:00:00+03:00",
        "first_period": False,
        "turnover": [
            {"kind": "subscription", "currency": "RUB",
             "gross": "10000", "sold": 20},
            {"kind": "file", "currency": "RUB",
             "gross": "4000", "sold": 10},
        ],
        "service_rub": "150", "subscription_rub": "500",
        "file_rub": "200", "total_rub": "850",
        "paid_rub": "0", "credit_rub": "0", "total_due_rub": "850",
    })
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", summary)
    old_index = AsyncMock(side_effect=AssertionError("No account-wide invoices"))
    monkeypatch.setattr(ui, "merchant_saas_statements", old_index)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    lang_token = _language.set(lang)
    try:
        q = _callback("saasinvoices:0")
        await _handler(project)(q, bot)
    finally:
        _language.reset(lang_token)
    summary.assert_awaited_once()
    old_index.assert_not_awaited()
    q.answer.assert_awaited_once()
    body = bot.send_message.await_args.args[1]
    buttons = [b for row in bot.send_message.await_args.kwargs[
        "reply_markup"].inline_keyboard for b in row]
    assert len(buttons) == 2
    assert buttons[0].callback_data == (
        "own:spm:" + encode_uuid(invoice) + ":RUB:0"
    )
    assert buttons[1].callback_data == "own:dashboard"
    assert "850.00 ₽" in body and "10000 RUB" in body
    assert str(invoice) not in body
    assert ("Продажи подписок" if lang == "ru" else
            "Subscription sales") in body


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_one_method_creates_only_full_payment_attempt(monkeypatch, lang):
    project, invoice, method = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "saas_v2_invoice_balance", AsyncMock(
        return_value={"remaining_rub": "850.00"},
    ))
    monkeypatch.setattr(ui, "saas_catalog_for_invoice", AsyncMock(
        return_value=({"id": str(method), "currency": "RUB", "name": "СБП"},),
    ))
    attempt = AsyncMock(return_value={
        "id": str(uuid4()), "currency": "RUB", "amount": "850.00",
        "rub_per_unit": "1", "instructions": "Pay bank 123",
    })
    monkeypatch.setattr(ui, "saas_v3_create_attempt", attempt)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    tok = encode_uuid(invoice)
    mid = encode_uuid(method)
    token = _language.set(lang)
    try:
        handler = _handler(project)
        await handler(_callback(f"spm:{tok}:RUB:0"), bot)
        assert bot.edit_message_text.await_count == 1
        assert bot.send_message.await_count == 0
        await handler(_callback(f"spv:{tok}:{mid}:RUB:0"), bot)
    finally:
        _language.reset(token)
    attempt.assert_awaited_once()
    assert attempt.await_args.kwargs["rub"] == 850
    assert "Pay bank 123" in bot.send_message.await_args.args[1]
    assert "own:saasinvoices:0" in str(
        bot.send_message.await_args.kwargs["reply_markup"]
    )
    assert "25%" not in str(bot.send_message.await_args.kwargs)
    assert "50%" not in str(bot.send_message.await_args.kwargs)


async def test_paid_bot_bill_has_only_main_menu(monkeypatch):
    project = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", AsyncMock(
        return_value={
            "invoice_id": None, "period_start": None, "period_end": None,
            "turnover": [], "service_rub": "0", "subscription_rub": "0",
            "file_rub": "0", "total_rub": "0", "paid_rub": "0",
            "credit_rub": "0", "total_due_rub": "0",
        },
    ))
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await _handler(project)(_callback("saasinvoices:0"), bot)
    rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 1
    assert rows[0][0].callback_data == "own:dashboard"
