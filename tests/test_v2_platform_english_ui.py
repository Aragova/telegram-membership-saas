"""RU/EN control-plane rendering retains data and server-side role boundaries."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_dashboard_handlers as platform_ui
from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.owner_setup.labels import (
    owner_button_label,
    owner_runtime_label,
)
from telegram_membership_saas.modules.owner_setup.service import OwnerHome
from telegram_membership_saas.modules.platform_dashboard import PlatformSnapshot
from telegram_membership_saas.modules.platform_usage import UsageSnapshot


def _platform_snapshot() -> PlatformSnapshot:
    return PlatformSnapshot(
        period="30", page=0, pages=1,
        counts={
            "bots": 2, "bots_active": 1, "bots_disabled": 1,
            "bots_running": 1, "owners": 2, "resources": 3,
            "plans_published": 4, "plans_draft": 1,
            "subscriptions_active": 5, "requests_pending": 2,
            "bots_new_period": 1, "subscriptions_new_period": 2,
            "payments_period": 3,
        },
        bots=({
            "username": "test_member_bot", "project_name": "My Custom Channel",
            "status": "registered", "runtime_state": "running",
            "resources": 1, "plans": 2, "subscriptions": 3,
            "payments_period": 3,
            "gmv_by_currency": [{"currency": "USD", "amount": "17.50"}],
        },),
        turnover=({"currency": "EUR", "amount": "120.00", "count": 3},),
    )


def test_platform_english_reporting_and_periods() -> None:
    token = _language.set("en")
    try:
        snapshot = _platform_snapshot()
        body = platform_ui._summary_text(snapshot)
        assert "Connected bots: <b>2</b>" in body
        assert "Client turnover by currency" in body
        assert "EUR" in body and "120" in body
        assert "billing is not yet enabled" in body
        assert "not verified" in body
        assert "Подключённых" not in body
        buttons = [b for row in platform_ui._period_rows("30", section="home")
                   for b in row]
        assert "✓ 30 days" in [b.text for b in buttons]
        assert "plat:home:30:0" in [b.callback_data for b in buttons]
        detail = platform_ui._bot_details(snapshot.bots[0], period="30")
        assert "My Custom Channel" in detail
        assert "Bot statistics" in detail
        assert "registered" in detail
        assert "Approved payments" in detail
        listing = platform_ui._bot_list(snapshot)
        assert "Connected bots" in listing
        assert "Page 1 of 1" in listing
        assert "@test_member_bot" not in listing
        assert "💎 plans" in listing and "🔐 subscriptions" in listing
        button = platform_ui._bot_list_button_label(snapshot.bots[0])
        assert button == "🟢 @test_member_bot · 💎2 🔐3 💳3"
    finally:
        _language.reset(token)


def test_platform_english_usage_keeps_history_and_currencies() -> None:
    sample = UsageSnapshot(
        period="30", page=0, pages=2, event_total=3,
        counts={"bot_removed": 1, "payment_approved": 2},
        turnover=({"currency": "USD", "amount": "9.99", "count": 2},),
        history=({
            "bot_username": "retired_demo_bot", "event_type": "bot_removed",
            "occurred_at": datetime(2026, 11, 4, 10, tzinfo=UTC).isoformat(),
        },),
    )
    token = _language.set("en")
    try:
        body = platform_ui._usage_text(sample, timezone="Europe/Moscow")
        assert "Bot removals: 1" in body
        assert "@retired_demo_bot: removed" in body
        assert "USD" in body and "9.99" in body
        assert "not ViMi Pay invoices" in body
        assert "Page 1 of 2" in body
    finally:
        _language.reset(token)


def test_owner_buttons_and_bot_health_are_bilingual() -> None:
    token = _language.set("en")
    try:
        assert owner_button_label("📦 Тарифы") == "📦 Plans"
        assert owner_button_label("⬅️ Мои подключения") == "⬅️ My connections"
        assert owner_runtime_label("🟢 Работает") == "🟢 Running"
        assert owner_button_label("🗑 Remove @demo") == "🗑 Remove @demo"
    finally:
        _language.reset(token)
    assert owner_button_label("📦 Тарифы") == "📦 Тарифы"
    assert owner_runtime_label("🟢 Работает") == "🟢 Работает"


async def test_english_central_welcome(monkeypatch) -> None:
    monkeypatch.setattr(
        owner_ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        owner_ui, "owner_home", AsyncMock(return_value=OwnerHome((), None)),
    )
    monkeypatch.setattr(owner_ui, "cancel_owner_flow", AsyncMock())
    router = owner_ui.create_owner_setup_router(MagicMock())
    start = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "home_message"
    )
    user = SimpleNamespace(id=7281, username="user", first_name="Test")
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=7281),
        from_user=user,
    )
    bot = MagicMock()
    bot.id = 12345
    bot.send_message = AsyncMock()
    bot.set_my_commands = AsyncMock()
    token = _language.set("en")
    try:
        await start(message, bot)
        sent = bot.send_message.await_args
        assert "Membership management" in sent.args[1]
        assert "Connect your Telegram bot" in sent.args[1]
        labels = [
            b.text for row in sent.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert "✨ Create a bot" in labels
        assert "🔑 Via BotFather" in labels
        assert "🌐 Language / Язык" in labels
    finally:
        _language.reset(token)
