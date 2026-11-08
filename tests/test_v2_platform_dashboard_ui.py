"""Platform/tenant mode separation, private access and compact operator UX."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.platform_dashboard import (
    PlatformSnapshot,
    fmt_turnover,
    is_configured_platform_owner,
    period_start,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid
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
    b.set_my_commands=AsyncMock()
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
    assert central.sub_routers[0].name == "vimi-language"
    assert central.sub_routers[1].name == "platform-dashboard"
    assert central.sub_routers[2].name == "secure-owner-transfer"
    assert central.sub_routers[3].name == "central-registrar"
    assert "platform-dashboard" not in [r.name for r in tenant.sub_routers]

    monkeypatch.setattr(adapter, "get_settings", lambda: _settings(None))
    central = create_dispatcher(MagicMock(), enable_access=False)
    assert central.sub_routers[0].name == "vimi-language"
    assert central.sub_routers[1].name == "secure-owner-transfer"
    assert central.sub_routers[2].name == "central-registrar"


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
    assert "my_premium_bot" not in bot.send_message.await_args.args[1]
    assert any(
        "@my_premium_bot" in button.text
        and "💎2 🔐3 💳3" in button.text
        for button in _buttons(bot)
    )
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
    assert fmt_turnover("12345678901234567890.123456789012345678") == (
        "12345678901234567890.123456789012345678"
    )


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


async def test_platform_menu_shows_three_pairs(monkeypatch):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "platform_report", AsyncMock(return_value=_snapshot()))
    bot = _bot()
    router = ui.create_platform_dashboard_router(MagicMock(), settings=_settings())
    await _start_handler(router)(_message(), bot)
    rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert [len(row) for row in rows[-3:]] == [2, 2, 2]
    assert [[b.callback_data for b in row] for row in rows[-3:]] == [
        ["plat:bots:30:0", "plat:usage:30:0"],
        ["plat:merchants:30:0", "plat:risks:30:0"],
        ["lng:menu", "own:projects"],
    ]


def test_rich_currency_turnover_and_single_bot_metrics():
    from telegram_membership_saas.rich_messages import to_rich_html

    summary = to_rich_html(ui._summary_text(_snapshot()))
    assert "• RUB — 1200 (операций: 3)" in summary
    assert "<th>• RUB" not in summary
    detail = to_rich_html(ui._bot_details(_snapshot().bots[0], period="30"))
    assert "<table bordered compact>" in detail
    assert "<tr><th>📢 Ресурсов</th><td>1</td></tr>" in detail
    assert "<tr><th>Работа</th><td>работает</td></tr>" in detail
    assert (
        "<tr><th>💳 Подтверждённых оплат (30 дней)</th><td>3</td></tr>"
        in detail
    )

    from telegram_membership_saas.localization import _language

    token = _language.set("en")
    try:
        detail_en = to_rich_html(ui._bot_details(_snapshot().bots[0], period="30"))
        summary_en = to_rich_html(ui._summary_text(_snapshot()))
    finally:
        _language.reset(token)
    assert "<tr><th>💳 Approved payments (30 days)</th><td>3</td></tr>" in detail_en
    assert "• RUB — 1200 (transactions: 3)" in summary_en
    assert "<th>• RUB" not in summary_en


async def test_platform_bot_list_details_and_back_edit_one_message(monkeypatch):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "platform_report", AsyncMock(return_value=_snapshot()))
    router = ui.create_platform_dashboard_router(MagicMock(), settings=_settings())
    handler = _callback_handler(router)
    bot = _bot()
    bot.edit_message_text = AsyncMock()
    for data in (
        "plat:bots:30:0",
        "plat:detail:30:0:0:" + encode_uuid(BOT_INSTANCE_ID),
        "plat:bots:30:0",
        "plat:home:30:0",
    ):
        query = _callback(data)
        query.message.message_id = 517
        await handler(query, bot)
        assert query.answer.await_count == 1
        assert bot.edit_message_text.await_args.kwargs["message_id"] == 517
    assert bot.edit_message_text.await_count == 4
    bot.send_message.assert_not_awaited()


async def test_platform_edit_falls_back_only_when_message_cannot_be_edited():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    bot = _bot()
    bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(
        method=EditMessageText(chat_id=7777, message_id=517, text="test"),
        message="Bad Request: message can't be edited",
    ))
    await ui._render_platform_screen(
        bot, chat_id=7777, message_id=517, text="test",
        reply_markup=ui._keyboard([[ui._button("Back", "home:30:0")]]),
    )
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize(("state", "emoji"), [
    ("running", "🟢"),
    ("starting", "🟡"),
    ("stale", "🟠"),
    ("stopped", "🔴"),
    ("failed", "🔴"),
    ("disabled", "⚪"),
    ("deleting", "⚪"),
    ("registered", "⚪"),
    ("unknown", "⚪"),
    ("unexpected", "⚪"),
])
def test_compact_bot_button_keeps_status_handle_and_exact_metrics(state, emoji):
    bot = dict(_snapshot().bots[0])
    bot["runtime_state"] = state
    bot["username"] = "telegram_long_bot_username_123456"
    bot["plans"] = 139
    bot["subscriptions"] = 6000
    bot["payments_period"] = 487
    result = ui._bot_list_button_label(bot)
    assert result.startswith(emoji + " @telegram_")
    assert result.endswith(" · 💎139 🔐6000 💳487")
    assert len(result) <= 64
    assert "<" not in result


@pytest.mark.parametrize(("language", "heading", "period"), [
    ("ru", "Подключённые боты", "30 дней"),
    ("en", "Connected bots", "30 days"),
])
def test_compact_list_contains_legend_page_but_no_duplicated_usernames(
    language, heading, period,
):
    from dataclasses import replace

    from telegram_membership_saas.localization import _language

    originals = _snapshot()
    sample = []
    for idx, status in enumerate(("running", "stale", "disabled", "failed")):
        item = dict(originals.bots[0])
        item.update(
            bot_instance_id=str(uuid4()), username=f"premium_customer_{idx}_bot",
            runtime_state=status, plans=idx + 2,
            subscriptions=idx * 3, payments_period=idx * 2,
        )
        sample.append(item)
    snapshot = replace(originals, bots=tuple(sample), page=1, pages=8)
    token = _language.set(language)
    try:
        summary = ui._bot_list(snapshot)
        labels = [ui._bot_list_button_label(item) for item in sample]
    finally:
        _language.reset(token)
    assert heading in summary and period in summary
    assert ("Страница 2 из 8" if language == "ru" else "Page 2 of 8") in summary
    assert "💎" in summary and "🔐" in summary and "💳" in summary
    assert all(item["username"] not in summary for item in sample)
    assert all(("@" + item["username"]) in label for item, label in zip(sample, labels))
    assert all(len(label) <= 64 for label in labels)
    assert all(item["plans"] >= 2 for item in sample)


async def test_compact_bot_list_pagination_and_rich_detail_edit_stay_stable(
    monkeypatch,
):
    from dataclasses import replace

    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    original = _snapshot(period="7", page=1)
    bot_item = dict(original.bots[0])
    bot_item.update(
        username="another_customer_bot", runtime_state="stale",
        plans=5, subscriptions=8, payments_period=11,
    )
    report = replace(original, bots=(bot_item,), pages=8)
    monkeypatch.setattr(ui, "platform_report", AsyncMock(return_value=report))
    router = ui.create_platform_dashboard_router(MagicMock(), settings=_settings())
    handler = _callback_handler(router)
    bot = _bot()
    bot.edit_message_text = AsyncMock()
    list_cb = _callback("plat:bots:7:1")
    list_cb.message.message_id = 517
    await handler(list_cb, bot)
    bot.send_message.assert_not_awaited()
    kwargs = bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 517
    assert "another_customer_bot" not in bot.edit_message_text.await_args.args[0]
    rows = kwargs["reply_markup"].inline_keyboard
    buttons = [button for row in rows for button in row]
    current_id = "plat:detail:7:1:0:" + encode_uuid(BOT_INSTANCE_ID)
    assert [button.callback_data for button in buttons] == [
        current_id, "plat:bots:7:0", "plat:bots:7:2", "plat:home:7:0",
    ]
    assert buttons[0].text.startswith("🟠 @another_customer_bot")
    assert "💎5 🔐8 💳11" in buttons[0].text

    detail_cb = _callback(current_id)
    detail_cb.message.message_id = 517
    await handler(detail_cb, bot)
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 517
    assert "Статистика бота" in bot.edit_message_text.await_args.args[0]
    assert bot.edit_message_text.await_args.kwargs[
        "reply_markup"
    ].inline_keyboard[0][0].callback_data == "plat:bots:7:1"
    bot.send_message.assert_not_awaited()


def test_compact_bot_list_handles_empty_page_without_actions():
    from dataclasses import replace

    empty = replace(_snapshot(), bots=(), page=0, pages=1)
    summary = ui._bot_list(empty)
    assert "На этой странице нет подключений." in summary
    assert "Страница 1 из 1" in summary
    assert "👇 Выберите бота" not in summary

