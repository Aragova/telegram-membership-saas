"""Historical account archive remains accessible in central bot after transfers."""
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
async def test_central_has_archive_but_no_cross_bot_live_invoice(
    monkeypatch, language,
):
    invoice = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    old = AsyncMock(return_value=MerchantSaasStatements(
        total=1, unpaid=1, outstanding_rub=Decimal("150"),
        configured=True, instructions=None, page=0,
        invoices=({"id": str(invoice), "kind": "prepayment",
                   "status": "issued", "amount_rub": "150.00"},),
    ))
    monkeypatch.setattr(ui, "merchant_saas_statements", old)
    monkeypatch.setattr(ui, "saas_price_profile", AsyncMock(
        return_value={"currency": "RUB", "monthly_amount": "150.00"},
    ))
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
    finally:
        _language.reset(token)
    old.assert_awaited_once()
    live.assert_not_awaited()
    buttons = [b.callback_data for row in bot.send_message.await_args.kwargs[
        "reply_markup"].inline_keyboard for b in row]
    assert "own:saasbill:" + encode_uuid(invoice) + ":0" in buttons
    assert "own:onboardhome" in buttons
    # Account invoices remain accessible, but the service currency is
    # selected only in each connected bot's Bill menu.
    assert "own:saasprofile" not in buttons
    assert not any(x.startswith("own:spm:") for x in buttons)
    text_body = bot.send_message.await_args.args[1]
    assert ("СЧЁТ АККАУНТА" if language == "ru" else "ACCOUNT BILL") in text_body
    assert ("Неоплачено: 1" if language == "ru"
            else "Unpaid: 1") in text_body
    invoice_button = next(
        b.text
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row
        if b.callback_data.startswith("own:saasbill:")
    )
    assert ("К оплате" if language == "ru" else "Amount due") in invoice_button
