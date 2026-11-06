"""Private platform-only channel risk review UI and callback tests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.platform_risk import (
    MerchantRiskSnapshot,
    platform_merchant_risk_report,
)


def _report():
    return MerchantRiskSnapshot(
        period="30", page=0, pages=2, total=7,
        observations=({
            "chat_id": "-1001234567890",
            "project_name": "Owner & Partner <Premium>\nProject",
            "channel_name": "My & <Private>\nChannel",
            "kind": "historical_chat_other_owner",
            "observed_at": "2026-11-06T12:15:00+00:00",
        },),
    )


def _callback(uid=7777, data="plat:risks:30:0"):
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


def test_risk_text_is_escaped_localized_and_not_fraud_verdict():
    ru = ui._risk_review_text(_report(), timezone="Europe/Moscow")
    assert "Проверка каналов" in ru
    assert "My &amp; &lt;Private&gt; Channel" in ru
    assert "Owner &amp; Partner &lt;Premium&gt; Project" in ru
    assert "<code>-1001234567890</code>" in ru
    assert "06.11.2026 15:15" in ru
    assert "Законная передача возможна" in ru
    assert "Страница 1 из 2" in ru

    token = _language.set("en")
    try:
        en = ui._risk_review_text(_report(), timezone="Europe/Moscow")
        assert "Channel review" in en
        assert "Legitimate transfers are possible" in en
        assert "Page 1 of 2" in en
    finally:
        _language.reset(token)


def test_empty_risk_report_is_not_false_security_assurance():
    empty = MerchantRiskSnapshot(
        period="all", page=0, pages=0, total=0, observations=(),
    )
    rendered = ui._risk_review_text(empty, timezone="Europe/Moscow")
    assert "Совпадений между разными владельцами нет" in rendered
    assert "ничего не блокирует автоматически" in rendered


async def test_risk_callbacks_private_server_identity_and_pagination(monkeypatch):
    get_risk = AsyncMock(return_value=_report())
    monkeypatch.setattr(ui, "platform_merchant_risk_report", get_risk)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=Settings(platform_owner_telegram_id=7777),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await _handler(router)(_callback(uid=8888), bot)
    get_risk.assert_not_awaited()
    bot.send_message.assert_not_awaited()

    await _handler(router)(_callback(), bot)
    get_risk.assert_awaited_once()
    assert get_risk.await_args.kwargs["actor_telegram_id"] == 7777
    calls = [
        button.callback_data
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "plat:risks:30:1" in calls
    assert "plat:home:30:0" in calls
    assert "plat:risks:all:0" in calls

    for bad in (
        "plat:risks:bad:0", "plat:risks:30:-1",
        "plat:risks:30:100001", "plat:risks:30:0.5",
    ):
        call = _callback(data=bad)
        await _handler(router)(call, bot)
        assert call.answer.await_args.kwargs["show_alert"] is True
    assert get_risk.await_count == 1


async def test_risk_report_rejects_non_platform_actor():
    with pytest.raises(PermissionError):
        await platform_merchant_risk_report(
            MagicMock(), actor_user_id=uuid4(), actor_telegram_id=7777,
            settings=Settings(platform_owner_telegram_id=None),
        )
