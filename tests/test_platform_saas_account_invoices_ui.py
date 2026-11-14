"""One unified account bill must remain reachable after transferring the final bot."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_central_account_bill_does_not_require_project(
    monkeypatch, language,
):
    actor_id, invoice_id = uuid4(), uuid4()
    identity = SimpleNamespace(id=979797, username="historical", first_name="Owner")
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    get_project = AsyncMock(side_effect=AssertionError(
        "Account bill callback must not look up a selected project"
    ))
    monkeypatch.setattr(ui, "get_selected_project", get_project)
    bill = AsyncMock(return_value={
        "month": "2026-11-01", "project_id": None,
        "turnover": [], "service_due_rub": "150",
        "commission_due_rub": "0", "total_due_rub": "150",
        "unpaid_count": 1, "first_invoice_id": str(invoice_id),
    })
    preview = AsyncMock(return_value={
        "rails": [], "unrated_billable": 0, "uncaptured_crypto": 0,
    })
    monkeypatch.setattr(ui, "merchant_saas_bill_summary", bill)
    monkeypatch.setattr(ui, "merchant_saas_unified_monthly_preview", preview)
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
    bill.assert_awaited_once()
    assert bill.await_args.kwargs["project_id"] is None
    assert preview.await_args.kwargs["project_id"] is None
    callback.answer.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    msg = bot.send_message.await_args.args[1]
    assert ("Счёт ViMi Pay" if language == "ru"
            else "ViMi Pay bill") in msg
    assert "150.00 ₽" in msg
    assert "TEST bank &amp; &lt;details&gt;" not in msg
    buttons = [
        b for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard for b in row
    ]
    assert len(buttons) == 2
    assert buttons[0].callback_data == (
        "own:spm:" + encode_uuid(invoice_id) + ":RUB:0"
    )
    assert buttons[1].callback_data == "own:projects"
    assert ("Главное меню" if language == "ru"
            else "Main menu") in buttons[1].text
