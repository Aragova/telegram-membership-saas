"""Historical ViMi Pay account invoices remain reachable after final bot transfer."""
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


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_central_account_invoice_button_does_not_require_project(
    monkeypatch, language,
):
    actor_id = uuid4()
    identity = SimpleNamespace(id=979797, username="historical", first_name="Owner")
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    get_project = AsyncMock(side_effect=AssertionError(
        "Account invoice callback must not look up a selected project"
    ))
    monkeypatch.setattr(ui, "get_selected_project", get_project)
    get_statements = AsyncMock(return_value=MerchantSaasStatements(
        total=1, unpaid=1, outstanding_rub=Decimal("150.00"),
        configured=True, instructions="TEST bank & <details>",
        invoices=({
            "id": str(uuid4()), "status": "unpaid",
            "kind": "prepayment", "amount_rub": "150.00",
            "due_at": "2026-11-15T00:00:00+00:00",
        },), page=0,
    ))
    monkeypatch.setattr(ui, "merchant_saas_statements", get_statements)
    monkeypatch.setattr(
        ui, "merchant_saas_trial_status",
        AsyncMock(return_value={"state": "ended"}),
    )
    monkeypatch.setattr(ui, "saas_price_profile", AsyncMock(
        return_value={"currency": "RUB", "monthly_amount": "150.00"},
    ))
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=None)
    callback_handler = next(
        handler.callback for handler in router.callback_query.handlers
        if handler.callback.__name__ == "owner_callback"
    )
    callback = SimpleNamespace(
        data="own:saasinvoices:0",
        from_user=identity,
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )
    bot = MagicMock()
    bot.id = 123456
    bot.send_message = AsyncMock()
    token = _language.set(language)
    try:
        await callback_handler(callback, bot)
    finally:
        _language.reset(token)
    get_project.assert_not_awaited()
    get_statements.assert_awaited_once()
    assert get_statements.await_args.kwargs["project_id"] is None
    callback.answer.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    msg = bot.send_message.await_args.args[1]
    assert "150.00" in msg
    assert "TEST bank &amp; &lt;details&gt;" in msg
    buttons = [
        b.callback_data
        for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for b in row
    ]
    assert "own:saasinvoices:0" in buttons
    assert "own:projects" in buttons
