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
