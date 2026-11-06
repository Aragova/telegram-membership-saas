"""Platform owner merchant-finance screen: compact RU/EN and private callbacks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.merchant_finances import (
    MerchantFinances,
    platform_merchant_finances,
)


def _snapshot():
    return MerchantFinances(
        period="30", page=0, pages=2, merchants_total=7, unmapped_payments=1,
        businesses=({
            "merchant_id": str(uuid4()),
            "label": "My & Premium <Channel>",
            "payments": 2,
            "currencies": [
                {"currency": "USD", "amount": "12.345", "count": 1},
                {"currency": "RUB", "amount": "500.00", "count": 1},
            ],
        },),
    )


def _callback(uid=7777, data="plat:merchants:30:0"):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=uid, username="tester", first_name="Test"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=uid)),
        answer=AsyncMock(),
    )


def _handler(router):
    return next(
        entry.callback for entry in router.callback_query.handlers
        if entry.callback.__name__ == "platform_callback"
    )


def test_merchant_financial_text_escapes_labels_and_preserves_currency():
    ru = ui._merchant_text(_snapshot())
    assert "Финансы бизнесов" in ru
    assert "My &amp; Premium &lt;Channel&gt;" in ru
    assert "12.345" in ru and "500" in ru
    assert "операций: 1" in ru
    assert "без установленного исторического бизнеса: 1" in ru
    assert "не выручка ViMi Pay" in ru
    assert "Страница 1 из 2" in ru

    token = _language.set("en")
    try:
        en = ui._merchant_text(_snapshot())
        assert "Merchant finances" in en
        assert "transactions: 1" in en
        assert "not independent bank verification" in en
    finally:
        _language.reset(token)


async def test_private_merchant_screen_uses_server_bound_owner_identity(monkeypatch):
    report = AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(ui, "platform_merchant_finances", report)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=Settings(platform_owner_telegram_id=7777),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await _handler(router)(_callback(uid=8888), bot)
    report.assert_not_awaited()
    bot.send_message.assert_not_awaited()

    await _handler(router)(_callback(), bot)
    report.assert_awaited_once()
    assert report.await_args.kwargs["actor_telegram_id"] == 7777
    callbacks = [
        button.callback_data
        for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for button in row
    ]
    assert "plat:merchants:30:1" in callbacks
    assert "plat:home:30:0" in callbacks

    for invalid in ("plat:merchants:BAD:0", "plat:merchants:30:-1",
                    "plat:merchants:30:100001"):
        callback = _callback(data=invalid)
        await _handler(router)(callback, bot)
        assert callback.answer.await_args.kwargs["show_alert"] is True
    assert report.await_count == 1


async def test_merchant_finances_rejects_unconfigured_owner():
    with pytest.raises(PermissionError):
        await platform_merchant_finances(
            MagicMock(), actor_user_id=uuid4(), actor_telegram_id=7777,
            settings=Settings(platform_owner_telegram_id=None),
        )
