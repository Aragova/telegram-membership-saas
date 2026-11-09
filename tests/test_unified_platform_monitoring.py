"""One monitoring surface inside the existing private platform dashboard.

No new global menu, second bot list, duplicated poller or account-facing
monitoring controls. Existing bot list/detail callbacks remain stable.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.platform_dashboard import PlatformSnapshot
from telegram_membership_saas.rich_messages import to_rich_html


def _snapshot(*, active=8, running=6, disabled=2):
    return PlatformSnapshot(
        period="30", page=0, pages=2,
        counts={
            "bots": active + disabled, "bots_active": active,
            "bots_running": running, "bots_disabled": disabled,
            "owners": 3, "resources": 5, "plans_published": 7,
            "plans_draft": 2, "subscriptions_active": 10,
            "requests_pending": 1, "bots_new_period": 1,
            "subscriptions_new_period": 3, "payments_period": 6,
        },
        bots=({
            "bot_instance_id": str(uuid4()), "username": "demo_bot",
            "project_name": "demo", "status": "registered",
            "runtime_state": "stale", "resources": 1, "plans": 2,
            "subscriptions": 3, "payments_period": 0,
            "gmv_by_currency": [],
        },),
        turnover=(),
    )


@pytest.mark.parametrize(("language", "title", "running", "unknown"), [
    ("ru", "Мониторинг · ViMi Pay", "Работают", "Нет подтверждения работы"),
    ("en", "Monitoring · ViMi Pay", "Running", "No confirmed runtime"),
])
def test_single_monitor_screen_uses_existing_health_and_native_rich_table(
    language, title, running, unknown,
):
    snap = _snapshot()
    token = _language.set(language)
    try:
        html = to_rich_html(ui._bot_list(snap))
        main = ui._summary_text(snap)
    finally:
        _language.reset(token)

    assert "<h3>" in html and title in html
    assert "<table bordered compact>" in html
    assert running in html and unknown in html
    assert "<td>6</td>" in html
    assert "<td>2</td>" in html  # Unconfirmed active bots, not assumed broken
    assert "<td>10</td>" in html
    assert "demo_bot" not in html  # Existing bot labels stay on their buttons
    assert "Страница 1" in html if language == "ru" else "Page 1" in html
    assert "Running now" not in main and "Работают сейчас" not in main
    assert "Disabled:" not in main and "Отключено:" not in main


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_platform_home_has_exactly_one_monitoring_entry_not_second_bot_menu(
    monkeypatch, language,
):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "platform_report", AsyncMock(return_value=_snapshot()))
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=SimpleNamespace(
            platform_owner_telegram_id=7777,
            platform_timezone="Europe/Moscow",
        ),
    )
    handler = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "platform_start"
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.set_my_commands = AsyncMock()
    user = SimpleNamespace(id=7777, username="operator", first_name="Operator")
    msg = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=7777),
        from_user=user,
    )
    token = _language.set(language)
    try:
        await handler(msg, bot)
    finally:
        _language.reset(token)
    keyboard = bot.send_message.await_args.kwargs["reply_markup"]
    items = [button for row in keyboard.inline_keyboard for button in row]
    monitor = [item for item in items if item.callback_data == "plat:bots:30:0"]
    assert len(monitor) == 1
    assert monitor[0].text == (
        "🖥 Мониторинг" if language == "ru" else "🖥 Monitoring"
    )
    assert not any(
        item.text in {"🤖 Все боты", "🤖 All bots", "🛡 Проблемы", "🛡 Problems"}
        for item in items
    )
    assert len(items) == 11  # Same existing menu size, no extra buttons


def test_no_signal_count_is_nonnegative_and_does_not_count_disabled_as_error():
    snap = _snapshot(active=0, running=0, disabled=9)
    body = ui._bot_list(snap)
    assert "Нет подтверждения работы: 0" in body
    assert "Отключено: 9" in body
    assert "Подключено: 9" in body
    assert "@demo_bot" not in body


# PR #110: UI diagnostics for the existing single monitoring screen.

def _diag_now():
    from datetime import UTC, datetime
    return datetime(2026, 11, 9, 18, tzinfo=UTC)


def _diag_live_now():
    from datetime import UTC, datetime
    return _diag_live_now()


def _diag_minutes(value):
    from datetime import timedelta
    return timedelta(minutes=value)


NOW = _diag_now()


def _diag_bot(state="running", *, heartbeat=NOW, error_code=None):
    return {
        "bot_instance_id": str(uuid4()),
        "username": "demo_bot",
        "project_name": "Test <VIP> & owner",
        "status": "registered",
        "runtime_state": state,
        "last_heartbeat": heartbeat.isoformat() if heartbeat is not None else None,
        "error_code": error_code,
        "resources": 1,
        "plans": 2,
        "subscriptions": 3,
        "payments_period": 1,
        "gmv_by_currency": [{"currency": "BTC", "amount": "0.00010000"}],
    }


@pytest.mark.parametrize(("state", "result"), [
    ("starting", "Запускается"),
    ("queued", "ожидает запуска"),
    ("setup_needed", "нужна настройка"),
])
def test_supervisor_starting_states_are_not_alarms(state, result):
    title, explanation = ui._monitoring_diagnosis(
        _diag_bot(state), now=NOW,
    )
    assert title.startswith("🟡 ")
    assert result.lower() in title.lower()
    assert "не подтверждённая авария" in explanation


def test_delayed_heartbeat_is_not_labeled_a_confirmed_outage():
    fresh_stale = _diag_bot(
        "stale", heartbeat=NOW - _diag_minutes(3),
    )
    title, body = ui._monitoring_diagnosis(fresh_stale, now=NOW)
    assert title.startswith("🟠") and "прогрев" in body

    old_stale = _diag_bot(
        "stale", heartbeat=NOW - _diag_minutes(40),
    )
    title, body = ui._monitoring_diagnosis(old_stale, now=NOW)
    assert title.startswith("🟠") and "не подтверждает аварию" in body


def test_stopped_bot_recently_restarted_is_not_false_red():
    now = _diag_live_now()
    recent = _diag_bot("stopped", heartbeat=now)
    assert ui._bot_list_button_label(recent).startswith("🟡 ")
    title, body = ui._monitoring_diagnosis(recent, now=now)
    assert title.startswith("🟡 ") and "перезапускается" in body

    stopped = _diag_bot("stopped", heartbeat=now - _diag_minutes(40))
    assert ui._bot_list_button_label(stopped).startswith("🔴 ")
    title, body = ui._monitoring_diagnosis(stopped, now=now)
    assert title.startswith("🔴 ")
    assert "недостаточно" in body


@pytest.mark.parametrize("code", ["worker_stopped", "lock_lost"])
def test_retryable_restart_codes_are_yellow_during_warmup(code):
    now = _diag_live_now()
    data = _diag_bot("error", heartbeat=now, error_code=code)
    assert ui._bot_list_button_label(data).startswith("🟡 ")
    title, body = ui._monitoring_diagnosis(data, now=now)
    assert title.startswith("🟡 ")
    assert "перезапуск" in body.lower() or "повторный запуск" in body.lower()


@pytest.mark.parametrize(("code", "expected"), [
    ("invalid_credentials", "токен"),
    ("telegram_check_failed", "Telegram"),
    ("missing_admin_rights", "прав"),
    ("startup_failed", "запуск"),
])
def test_known_supervisor_error_codes_have_safe_localized_explanations(code, expected):
    data = _diag_bot("error", error_code=code)
    title, explanation = ui._monitoring_diagnosis(data, now=NOW)
    assert title.startswith("🔴")
    assert expected.lower() in explanation.lower()
    assert ui._bot_list_button_label(data).startswith("🔴 ")


def test_no_raw_error_content_is_leaked_and_missing_heartbeat_is_unconfirmed():
    bad = _diag_bot("error", heartbeat=None, error_code="<script>token=secret</script>")
    title, explanation = ui._monitoring_diagnosis(bad, now=NOW)
    assert title.startswith("🔴")
    assert "secret" not in explanation and "<script>" not in explanation

    unknown = _diag_bot("running", heartbeat=None)
    title, explanation = ui._monitoring_diagnosis(unknown, now=NOW)
    assert title.startswith("⚪ ") and "подтверждения" in explanation


def test_platform_operator_detail_reuses_same_card_and_rich_with_native_last_seen():
    token = _language.set("ru")
    try:
        html = ui._bot_details(_diag_bot("error", error_code="missing_admin_rights"),
                               period="30", timezone="Europe/Moscow")
        rich = to_rich_html(html)
    finally:
        _language.reset(token)
    assert "Статистика бота" in html
    assert "Последний сигнал:" in html
    assert "09.11.2026 21:00" in html
    assert "<tg-time" in html
    assert "прав администратора" in html
    assert "BTC" in html and "0.0001" in html
    assert "Test &lt;VIP&gt; &amp; owner" in html
    assert "<table bordered compact>" in rich
    assert "💳 Подтверждённых оплат" in rich

    en_token = _language.set("en")
    try:
        en = ui._bot_details(_diag_bot("error", error_code="missing_admin_rights"),
                             period="30", timezone="Europe/Moscow")
    finally:
        _language.reset(en_token)
    assert "Last heartbeat:" in en
    assert "missing channel administrator rights" in en


def test_existing_bot_actions_and_no_worker_modules_added():
    # Unchanged action labels and legacy callbacks; no redundant monitoring menu.
    assert ui._button("Open", "bots:30:0").callback_data == "plat:bots:30:0"
    assert ui._button("Open", "detail:30:0:0:abc").callback_data == (
        "plat:detail:30:0:0:abc"
    )
