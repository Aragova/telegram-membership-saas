"""The subscription buyer must actually see a localized SaaS hold notice."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from telegram_membership_saas import localization
from telegram_membership_saas.modules.platform_saas_hold_errors import (
    CommercialHoldError,
    translate_commercial_hold,
)
from telegram_membership_saas.modules.telegram_commerce import handlers as checkout
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


@pytest.mark.parametrize(
    ("language", "expected", "other"),
    [
        ("ru", "Свяжитесь с администратором канала.", "Contact the channel"),
        ("en", "Contact the channel administrator.", "Свяжитесь с администратором"),
    ],
)
async def test_buyer_checkout_shows_verified_hold_notice_in_own_language(
    monkeypatch, language, expected, other,
):
    @translate_commercial_hold
    async def blocked_checkout(*_args, **_kwargs):
        raise DBAPIError(
            "INSERT INTO payment_requests", {},
            SimpleNamespace(sqlstate="P6711"),
        )

    @asynccontextmanager
    async def scoped(_factory, _project):
        yield MagicMock()

    monkeypatch.setattr(checkout, "system_project_session_scope", scoped)
    monkeypatch.setattr(
        checkout, "create_checkout_for_telegram_user", blocked_checkout,
    )
    router = checkout.create_telegram_commerce_router(
        MagicMock(), project_id=uuid4(),
    )
    handler = next(
        entry.callback for entry in router.callback_query.handlers
        if entry.callback.__name__ == "buy_handler"
    )
    callback = SimpleNamespace(
        data="buy:" + encode_uuid(uuid4()) + ":" + encode_uuid(uuid4()),
        from_user=SimpleNamespace(
            id=12345, username="buyer", first_name="Buyer",
        ),
        answer=AsyncMock(),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    token = localization._language.set(language)
    try:
        await handler(callback, bot)
    finally:
        localization._language.reset(token)
    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.await_args
    actual = args[0]
    assert kwargs["show_alert"] is True
    assert expected in actual
    assert other not in actual
    assert "Request unavailable" not in actual
    assert len(actual) < 200
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("language", ["ru", "en"])
def test_hold_error_type_is_safe_and_other_english_errors_are_hidden(language):
    token = localization._language.set(language)
    try:
        notice = CommercialHoldError(localization.tr(
            "Неоплаченный счёт ViMi Pay", "Overdue ViMi Pay invoice",
        ))
        assert checkout._buyer_error(notice) == str(notice)
        if language == "en":
            secret = ValueError("secret server SQL and personal bank data")
            assert checkout._buyer_error(secret) == (
                "Request unavailable. Please reopen /plans and retry."
            )
    finally:
        localization._language.reset(token)
