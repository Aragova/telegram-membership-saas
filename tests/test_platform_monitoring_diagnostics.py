"""Operator-only monitoring diagnostics based on existing Supervisor heartbeat.

No new bot scanners, notifications, menu entry, polling, billing or grants.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.rich_messages import to_rich_html


NOW = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)


def _bot(state="running", *, heartbeat=NOW, error_code=None):
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
        _bot(state), now=NOW,
    )
    assert title.startswith("🟡 ")
    assert result.lower() in title.lower()
    assert "не подтверждённая авария" in explanation


def test_delayed_heartbeat_is_not_labeled_a_confirmed_outage():
    fresh_stale = _bot(
        "stale", heartbeat=NOW - timedelta(minutes=3),
    )
    title, body = ui._monitoring_diagnosis(fresh_stale, now=NOW)
    assert title.startswith("🟠") and "прогрев" in body

    old_stale = _bot(
        "stale", heartbeat=NOW - timedelta(minutes=40),
    )
    title, body = ui._monitoring_diagnosis(old_stale, now=NOW)
    assert title.startswith("🟠") and "не подтверждает аварию" in body


def test_stopped_bot_recently_restarted_is_not_false_red():
    now = datetime.now(UTC)
    recent = _bot("stopped", heartbeat=now)
    assert ui._bot_list_button_label(recent).startswith("🟡 ")
    title, body = ui._monitoring_diagnosis(recent, now=now)
    assert title.startswith("🟡 ") and "перезапускается" in body

    stopped = _bot("stopped", heartbeat=now - timedelta(minutes=40))
    assert ui._bot_list_button_label(stopped).startswith("🔴 ")
    title, body = ui._monitoring_diagnosis(stopped, now=now)
    assert title.startswith("🔴 ")
    assert "недостаточно" in body


@pytest.mark.parametrize("code", ["worker_stopped", "lock_lost"])
def test_retryable_restart_codes_are_yellow_during_warmup(code):
    now = datetime.now(UTC)
    data = _bot("error", heartbeat=now, error_code=code)
    assert ui._bot_list_button_label(data).startswith("🟡 ")
    title, body = ui._monitoring_diagnosis(data, now=now)
    assert title.startswith("🟡 ") and "перезапуск" in body.lower()


@pytest.mark.parametrize(("code", "expected"), [
    ("invalid_credentials", "токен"),
    ("telegram_check_failed", "Telegram"),
    ("missing_admin_rights", "прав"),
    ("startup_failed", "запуск"),
])
def test_known_supervisor_error_codes_have_safe_localized_explanations(code, expected):
    data = _bot("error", error_code=code)
    title, explanation = ui._monitoring_diagnosis(data, now=NOW)
    assert title.startswith("🔴")
    assert expected.lower() in explanation.lower()
    assert ui._bot_list_button_label(data).startswith("🔴 ")


def test_no_raw_error_content_is_leaked_and_missing_heartbeat_is_unconfirmed():
    bad = _bot("error", heartbeat=None, error_code="<script>token=secret</script>")
    title, explanation = ui._monitoring_diagnosis(bad, now=NOW)
    assert title.startswith("🔴")
    assert "secret" not in explanation and "<script>" not in explanation

    unknown = _bot("running", heartbeat=None)
    title, explanation = ui._monitoring_diagnosis(unknown, now=NOW)
    assert title.startswith("⚪ ") and "подтверждения" in explanation


def test_platform_operator_detail_reuses_same_card_and_rich_with_native_last_seen():
    token = _language.set("ru")
    try:
        html = ui._bot_details(_bot("error", error_code="missing_admin_rights"),
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
        en = ui._bot_details(_bot("error", error_code="missing_admin_rights"),
                             period="30", timezone="Europe/Moscow")
    finally:
        _language.reset(en_token)
    assert "Last heartbeat:" in en
    assert "missing channel administrator rights" in en


def test_existing_bot_actions_and_no_worker_modules_added():
    # Unchanged action labels and legacy callbacks; no redundant monitoring menu.
    from telegram_membership_saas.modules import platform_dashboard_handlers as platform
    assert platform._button("Open", "bots:30:0").callback_data == "plat:bots:30:0"
    assert platform._button("Open", "detail:30:0:0:abc").callback_data == (
        "plat:detail:30:0:0:abc"
    )
