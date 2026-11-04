"""Stage E: read-only platform usage UI with owner-only callbacks."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.platform_usage import (
    UsageSnapshot,
    platform_usage_report,
)


def _settings(owner=7777):
    return Settings(
        platform_owner_telegram_id=owner,
        platform_timezone="Europe/Moscow",
    )


def _usage():
    return UsageSnapshot(
        period="30", page=0, pages=2, event_total=12,
        counts={
            "bot_connected": 3,
            "bot_removed": 1,
            "bot_deleting": 1,
            "payment_approved": 5,
            "customer_joined": 2,
            "subscription_started": 2,
        },
        turnover=(
            {"currency": "RUB", "amount": "1000.00", "count": 4},
            {"currency": "USD", "amount": "9.99", "count": 1},
        ),
        history=(
            {
                "id": 98, "event_type": "bot_removed",
                "bot_username": "old_member_bot",
                "bot_id": str(uuid4()),
                "occurred_at": datetime(2026, 11, 4, 10, 0, tzinfo=UTC).isoformat(),
            },
        ),
    )


def _callback(user_id=7777, data="plat:usage:30:0"):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(
            id=user_id, username="tester", first_name="Test",
        ),
        message=SimpleNamespace(chat=SimpleNamespace(
            type="private", id=user_id,
        )),
        answer=AsyncMock(),
    )


def _handler(router):
    return next(
        entry.callback for entry in router.callback_query.handlers
        if entry.callback.__name__ == "platform_callback"
    )


def test_usage_screen_lists_deleted_bots_and_no_billing_claims():
    text = ui._usage_text(_usage(), timezone="Europe/Moscow")
    assert "@old_member_bot: удалён" in text
    assert "Удалений ботов: 1" in text
    assert "1000" in text and "RUB" in text and "USD" in text
    assert "не являются выставленными счетами" in text
    assert "Страница 1 из 2" in text


async def test_platform_usage_menu_is_operator_only(monkeypatch):
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=_settings(),
    )
    lookup = AsyncMock(return_value=_usage())
    monkeypatch.setattr(ui, "platform_usage_report", lookup)
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    outsider = _callback(user_id=8888)
    await _handler(router)(outsider, bot)
    assert outsider.answer.await_args.kwargs["show_alert"] is True
    lookup.assert_not_awaited()
    bot.send_message.assert_not_awaited()

    owner = _callback()
    await _handler(router)(owner, bot)
    lookup.assert_awaited_once()
    assert lookup.await_args.kwargs["actor_telegram_id"] == 7777
    assert "Удалений ботов" in bot.send_message.await_args.args[1]
    callbacks = [
        button.callback_data
        for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard
        for button in row
    ]
    assert "plat:usage:30:1" in callbacks
    assert "plat:home:30:0" in callbacks


async def test_usage_rejects_invalid_period_and_page_without_db_read(monkeypatch):
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=_settings(),
    )
    lookup = AsyncMock(return_value=_usage())
    monkeypatch.setattr(ui, "platform_usage_report", lookup)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    for query in ("plat:usage:BAD:0", "plat:usage:30:-1",
                  "plat:usage:30:100001"):
        callback = _callback(data=query)
        await _handler(router)(callback, bot)
        assert callback.answer.await_args.kwargs["show_alert"] is True
    lookup.assert_not_awaited()


async def test_platform_usage_report_rejects_unconfigured_owner():
    with pytest.raises(PermissionError):
        await platform_usage_report(
            MagicMock(), actor_user_id=uuid4(), actor_telegram_id=7777,
            settings=_settings(owner=None),
        )
