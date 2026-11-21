"""Historical account invoices remain accessible after transfers: approved new UX."""
from decimal import Decimal
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


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_account_overview_and_issued_invoice_navigation(monkeypatch, language):
    invoice = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    historical = AsyncMock(return_value=MerchantSaasStatements(
        total=1, unpaid=1, outstanding_rub=Decimal("150"),
        configured=True, instructions=None, page=0,
        invoices=({"id": str(invoice), "kind": "prepayment",
                   "status": "issued", "amount_rub": "150.00",
                   "remaining_rub": "150.00", "due_at": "2026-12-01"},),
    ))
    monkeypatch.setattr(ui, "merchant_saas_statements", historical)
    fleet = AsyncMock(return_value={
        "network": {}, "revenue": [],
        "billing": {
            "unpaid": 1, "overdue": 0, "overdue_rub": "0",
            "next": {
                "invoice_id": str(invoice), "username": "vimitest21bot",
                "due_at": "2026-12-01", "remaining_rub": "150",
                "same_day_count": 1,
            },
        },
    })
    monkeypatch.setattr(ui, "read_owner_network_home", fleet)
    live = AsyncMock(side_effect=AssertionError(
        "Central bot must not aggregate a live merchant invoice",
    ))
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", live)
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=None)
    callback = next(h.callback for h in router.callback_query.handlers
                    if h.callback.__name__ == "owner_callback")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    q = SimpleNamespace(
        data="own:saasinvoices:0",
        from_user=SimpleNamespace(id=42, username=None, first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=42)),
        answer=AsyncMock(),
    )
    token = _language.set(language)
    try:
        await callback(q, bot)
        assert (
            ("СЧЁТ АККАУНТА" if language == "ru" else "ACCOUNT BILL")
            in bot.send_message.await_args.args[1]
        )
        body = bot.send_message.await_args.args[1]
        assert "150 ₽" in body and "01.12.2026" in body
        assert str(invoice) not in body
        controls = [b.callback_data for row in bot.send_message.await_args.kwargs[
            "reply_markup"].inline_keyboard for b in row]
        assert "own:saasnearest:" + encode_uuid(invoice) in controls
        assert "own:saasaccountlist:0" in controls
        assert "own:projects" in controls and "own:onboardhome" in controls
        assert not any(x.startswith("own:spm:") for x in controls)
        assert "own:saasprofile" not in controls

        bot.reset_mock()
        q.data = "own:saasaccountlist:0"
        await callback(q, bot)
        buttons = [b.callback_data for row in bot.send_message.await_args.kwargs[
            "reply_markup"].inline_keyboard for b in row]
        assert "own:saasbill:" + encode_uuid(invoice) + ":0" in buttons
        assert "own:saasinvoices:0" in buttons
        assert not any(x.startswith("own:spm:") for x in buttons)
        assert historical.await_count == 2
        assert fleet.await_count == 1
    finally:
        _language.reset(token)
    live.assert_not_awaited()


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_account_without_debt_has_no_fake_nearest_bill(monkeypatch, language):
    monkeypatch.setattr(ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "merchant_saas_statements", AsyncMock(
        return_value=MerchantSaasStatements(
            total=1, unpaid=0, outstanding_rub=Decimal("0"),
            configured=False, instructions=None,
            invoices=({"id": str(uuid4()), "status": "paid",
                       "amount_rub": "150.00"},), page=0,
        ),
    ))
    monkeypatch.setattr(ui, "read_owner_network_home", AsyncMock(
        return_value={"billing": {
            "unpaid": 0, "overdue": 0, "overdue_rub": "0", "next": None,
        }},
    ))
    router = ui.create_owner_setup_router(MagicMock())
    callback = next(h.callback for h in router.callback_query.handlers
                    if h.callback.__name__ == "owner_callback")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    q = SimpleNamespace(
        data="own:saasinvoices:0",
        from_user=SimpleNamespace(id=42, username=None, first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=42)),
        answer=AsyncMock(),
    )
    token = _language.set(language)
    try:
        await callback(q, bot)
    finally:
        _language.reset(token)
    body = bot.send_message.await_args.args[1]
    assert ("НЕТ СЧЕТОВ К ОПЛАТЕ" if language == "ru"
            else "NO INVOICES TO PAY") in body
    assert "0 ₽" in body
    controls = [b.callback_data for row in bot.send_message.await_args.kwargs[
        "reply_markup"].inline_keyboard for b in row]
    assert controls == ["own:projects", "own:onboardhome"]
