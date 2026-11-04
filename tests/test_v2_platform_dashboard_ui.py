"""Platform/tenant mode separation, private access and compact operator UX."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid
from telegram_membership_saas.modules.platform_dashboard import (
    PlatformSnapshot,
    fmt_turnover,
    is_configured_platform_owner,
    period_start,
)
from telegram_membership_saas.telegram_adapter import (
    create_customer_dispatcher,
    create_dispatcher,
)


def _settings(owner=7777):
    return Settings(
        platform_owner_telegram_id=owner,
        platform_timezone="Europe/Moscow",
    )


BOT_INSTANCE_ID = uuid4()


def _snapshot(period="30", page=0):
    return PlatformSnapshot(
        period=period, page=page, pages=1,
        counts={
            "bots": 2, "bots_active": 1, "bots_disabled": 1,
            "bots_running": 1,
            "owners": 2, "resources": 3, "plans_published": 4,
            "plans_draft": 1, "subscriptions_active": 5,
            "requests_pending": 2, "bots_new_period": 1,
            "subscriptions_new_period": 2, "payments_period": 3,
        },
        bots=({
            "bot_instance_id": str(BOT_INSTANCE_ID),
            "username": "my_premium_bot", "project_name": "My channel",
            "status": "registered", "runtime_state": "running",
            "resources": 1, "plans": 2, "subscriptions": 3,
            "payments_period": 3,
            "gmv_by_currency": [{"currency": "USD", "amount": "17.50"}],
        },),
        turnover=({"currency": "RUB", "amount": "1200.00", "count": 3},),
    )


def _user(user_id=7777):
    return SimpleNamespace(id=user_id, username="platform", first_name="Owner")


def _message(user_id=7777):
    return SimpleNamespace(
        chat=SimpleNamespace(type="private", id=user_id),
        from_user=_user(user_id),
        text="/start",
    )


def _callback(data, user_id=7777):
    return SimpleNamespace(
        data=data,
        from_user=_user(user_id),
        message=SimpleNamespace(chat=SimpleNamespace(
            id=user_id, type="private",
        )),
        answer=AsyncMock(),
    )


def _bot():
    b=MagicMock()
    b.send_message=AsyncMock()
    return b


def _callback_handler(router):
    return next(h.callback for h in router.callback_query.handlers
                if h.callback.__name__ == "platform_callback")


def _start_handler(router):
    return next(h.callback for h in router.message.handlers
                if h.callback.__name__ == "platform_start")


def _buttons(bot):
    return [
        b for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard for b in row
    ]


def test_platform_router_only_in_central_and_only_when_configured(monkeypatch):
    import telegram_membership_saas.telegram_adapter as adapter

    monkeypatch.setattr(adapter, "get_settings", lambda: _settings())
    central = create_dispatcher(MagicMock(), enable_access=False)
    tenant = create_customer_dispatcher(MagicMock(), project_id=uuid4())
    assert central.sub_routers[0].name == "platform-dashboard"
    assert central.sub_routers[1].name == "central-registrar"
    assert "platform-dashboard" not in [r.name for r in tenant.sub_routers]

    monkeypatch.setattr(adapter, "get_settings", lambda: _settings(None))
    central = create_dispatcher(MagicMock(), enable_access=False)
    assert central.sub_routers[0].name == "central-registrar"


@pytest.mark.parametrize(("id_value", "expected"), [
    (7777, True), (8888, False),
])
def test_platform_owner_identity_is_explicit(id_value, expected):
    assert is_configured_platform_owner(id_value, _settings()) is expected
    assert not is_configured_platform_owner(id_value, _settings(None))


async def test_owner_start_filter_rejects_regular_tenant():
    gate = ui.PlatformOwnerStartFilter(_settings())
    assert await gate(_message()) is True
    assert await gate(_message(8888)) is False


async def test_operator_start_shows_separate_tenant_gmv_and_owner_menu(monkeypatch):
    actor_id = uuid4()
    read = AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(ui, "ensure_telegram_actor",
                        AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "platform_report", read)
    router=ui.create_platform_dashboard_router(MagicMock(),settings=_settings())
    bot=_bot()
    await _start_handler(router)(_message(),bot)
    text=bot.send_message.await_args.args[1]
    assert "Платформа" in text
    assert "Подключённых ботов: <b>2</b>" in text
    assert "1200" in text and "RUB" in text
    assert "биллинг ещё не запущен" in text
    assert "не проверены ViMi Pay" in text
    assert "own:projects" in [b.callback_data for b in _buttons(bot)]
    assert read.await_args.kwargs["actor_telegram_id"] == 7777
    assert read.await_args.kwargs["actor_user_id"] == actor_id


async def test_forged_platform_button_denied_without_db_access(monkeypatch):
    read=AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(ui, "platform_report", read)
    router=ui.create_platform_dashboard_router(MagicMock(),settings=_settings())
    callback=_callback("plat:home",user_id=8888)
    await _callback_handler(router)(callback,_bot())
    read.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_operator_navigation_periods_and_per_bot_details(monkeypatch):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    read=AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(ui, "platform_report", read)
    router=ui.create_platform_dashboard_router(MagicMock(),settings=_settings())
    callback=_callback("plat:home:7:0")
    bot=_bot()
    await _callback_handler(router)(callback,bot)
    assert read.await_args.kwargs["period"] == "7"
    callbacks=[b.callback_data for b in _buttons(bot)]
    assert "plat:home:30:0" in callbacks
    assert "plat:bots:7:0" in callbacks

    callback=_callback("plat:bots:7:0")
    await _callback_handler(router)(callback,bot)
    assert "my_premium_bot" in bot.send_message.await_args.args[1]
    stable = "plat:detail:7:0:0:" + encode_uuid(BOT_INSTANCE_ID)
    assert stable in [
        b.callback_data for b in _buttons(bot)
    ]
    callback=_callback(stable)
    await _callback_handler(router)(callback,bot)
    assert "Статистика бота" in bot.send_message.await_args.args[1]
    assert "17.5" in bot.send_message.await_args.args[1]


async def test_stale_platform_detail_fails_closed(monkeypatch):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "platform_report", AsyncMock(return_value=_snapshot()))
    router=ui.create_platform_dashboard_router(MagicMock(),settings=_settings())
    callback=_callback(
        "plat:detail:7:0:5:" + encode_uuid(uuid4())
    )
    await _callback_handler(router)(callback,_bot())
    assert callback.answer.await_args.kwargs["show_alert"] is True


def test_moscow_day_boundary_and_fixed_timezones():
    now=datetime(2026, 11, 4, 0, 30, tzinfo=UTC)
    assert period_start("day",settings=_settings(),now=now) == datetime(
        2026, 11, 3, 21, 0, tzinfo=UTC,
    )
    with pytest.raises(ValueError):
        period_start("wrong",settings=_settings(),now=now)
    assert fmt_turnover("1000.000000000000") == "1000"


async def test_bot_detail_rejects_stale_list_row_after_reordering(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    shifted = _snapshot()
    other = dict(shifted.bots[0])
    other["bot_instance_id"] = str(uuid4())
    other["username"] = "another_customer_bot"
    monkeypatch.setattr(
        ui, "platform_report",
        AsyncMock(return_value=replace(shifted, bots=(other,))),
    )
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=_settings(),
    )
    bot = _bot()
    original = "plat:detail:30:0:0:" + encode_uuid(BOT_INSTANCE_ID)
    callback = _callback(original)
    await _callback_handler(router)(callback, bot)
    bot.send_message.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True
