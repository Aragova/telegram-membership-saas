"""Unconfigured external SaaS gateways cannot create financial payment attempts.

Real provider settlement and platform Crypto app must
be implemented and verified before this draft invoice menu may ship.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


@pytest.mark.parametrize("language", ("ru", "en"))
@pytest.mark.parametrize("rail", ("L", "C"))
async def test_dormant_gateway_never_creates_invoice_payment(
    monkeypatch, language, rail,
):
    actor, invoice = uuid4(), uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(
        ui, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    payment = AsyncMock()
    catalog = AsyncMock()
    monkeypatch.setattr(ui, "saas_v3_create_attempt", payment)
    monkeypatch.setattr(ui, "saas_catalog_for_invoice", catalog)
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=None)
    callback = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    bot = MagicMock()
    bot.id = 1234
    bot.send_message = AsyncMock()
    query = SimpleNamespace(
        from_user=SimpleNamespace(
            id=9871, username="payer", first_name="Owner",
        ),
        data="own:spr:" + encode_uuid(invoice) + ":" + rail + ":0",
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=9871)),
        answer=AsyncMock(),
    )
    locale = _language.set(language)
    try:
        await callback(query, bot)
        assert bot.send_message.await_count == 1
        message = bot.send_message.await_args.args[1]
        assert ("недоступна" if language == "ru" else "not available") in message
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data == (
            "own:spm:" + encode_uuid(invoice) + ":RUB:0"
        )
    finally:
        _language.reset(locale)
    payment.assert_not_awaited()
    catalog.assert_not_awaited()
    query.answer.assert_awaited_once()
