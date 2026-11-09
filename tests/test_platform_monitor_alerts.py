"""Real PostgreSQL owner-only incident ledger and safe alert delivery.

All tests use existing Supervisor heartbeat; no second bot poller or user menus.
"""
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.platform_monitor_alerts import (
    PlatformMonitorIncident,
    PlatformMonitorOutbox,
    _alert_text,
    _healthy,
    _problem_threshold,
    deliver_platform_monitor_event,
    scan_platform_monitor,
)
from telegram_membership_saas.tenant_context import TenantContext

DB_AVAILABLE = bool(
    os.getenv("TMS_DATABASE_URL") and os.getenv("TMS_MIGRATION_DATABASE_URL")
)


def _fresh_id():
    return (uuid4().int % 7_000_000_000_000_000_000) + 1


def test_monitoring_thresholds_do_not_raise_false_alarms_during_normal_boot():
    moment = datetime(2026, 11, 9, 18, tzinfo=UTC)
    base = {"status": "registered", "runtime_state": "running",
            "last_heartbeat": moment.isoformat()}
    assert _healthy(base, moment)
    assert not _healthy({**base, "runtime_state": "starting"}, moment)
    assert not _healthy({**base, "last_heartbeat": None}, moment)
    assert _problem_threshold({**base, "status": "disabled"}) is None
    assert _problem_threshold({**base, "runtime_state": "setup_needed"}) is None
    assert _problem_threshold({**base, "runtime_state": "starting"}) == (
        timedelta(minutes=20)
    )
    assert _problem_threshold({**base, "runtime_state": "error"}) == (
        timedelta(minutes=10)
    )


def test_safe_notification_reason_and_one_existing_monitor_callback():
    now = datetime(2026, 11, 9, 18, tzinfo=UTC)
    from telegram_membership_saas.localization import _language

    token = _language.set("ru")
    try:
        text_value = _alert_text(
            "problem", username="danger<script>",
            status="error", error_code="missing_admin_rights",
            heartbeat=now, since=now, timezone="Europe/Moscow",
        )
        assert "&lt;script&gt;" in text_value
        assert "прав администратора" in text_value
        assert "<tg-time" in text_value
        assert "token" not in text_value

        recovered = _alert_text(
            "recovered", username="bot", status="running",
            error_code=None, heartbeat=now, since=now, timezone="Europe/Moscow",
        )
        assert "восстановлена" in recovered
        assert "прав администратора" not in recovered
    finally:
        _language.reset(token)


def test_success_card_no_longer_duplicates_status_or_project():
    from telegram_membership_saas.modules.platform_dashboard_handlers import (
        _bot_details,
    )

    now = datetime.now(UTC)
    data = {
        "username": "premium_bot", "project_name": "@premium_bot",
        "status": "registered", "runtime_state": "running",
        "last_heartbeat": now.isoformat(), "error_code": None,
        "resources": 1, "plans": 2, "subscriptions": 3,
        "payments_period": 1, "gmv_by_currency": [],
    }
    body = _bot_details(data, period="30")
    assert "🟢 Состояние: Работает" in body
    assert "Последний сигнал:" in body
    assert "Проект:" not in body
    assert "Подключение:" not in body
    assert "Диагностика:" not in body
    assert "Последний записанный статус" not in body
    assert body.count("Работает") == 1


@pytest.mark.skipif(not DB_AVAILABLE, reason="Requires real PostgreSQL with owner role")
async def test_operator_incident_one_problem_one_recovery_and_tenant_isolation():
    settings = Settings()
    admin_engine = create_async_engine(settings.migration_database_url)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    owner_tg, other_tg, bot_tg = _fresh_id(), _fresh_id(), _fresh_id()
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=owner_tg, username=None, first_name="Owner",
        )
        other = await ensure_telegram_actor(
            factory, telegram_id=other_tg, username=None, first_name="Other",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Alert test",
        )
        now = datetime.now(UTC)
        old = now - timedelta(minutes=12)

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            instance = BotInstance(
                project_id=project, telegram_bot_id=bot_tg,
                username="test_alert_bot", display_name="Test",
                status="registered", token_ciphertext="encrypted-test-value",
            )
            session.add(instance)
            await session.flush()
            bot_id = instance.id
            session.add(CustomerBotRuntimeHealth(
                project_id=project, telegram_bot_id=bot_tg,
                instance_id=uuid4(), state="error",
                error_code="missing_admin_rights", last_heartbeat=old,
            ))
        async with admin_engine.begin() as admin:
            await admin.execute(
                text("INSERT INTO platform_operators(telegram_user_id, role) "
                     "VALUES (:id, 'owner')"), {"id": owner_tg},
            )

        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=other),
            ) as session:
                await session.execute(
                    text("SELECT public.platform_monitor_health_snapshot()")
                )
        # Ordinary project owners cannot read the cross-project incident ledger.
        async with tenant_session_scope(
            factory, TenantContext(user_id=other),
        ) as session:
            assert (await session.scalars(select(PlatformMonitorIncident))).all() == []

        assert await scan_platform_monitor(
            factory, owner_actor_id=owner, now=old,
        ) == 0  # one observation must never send an alert
        assert await scan_platform_monitor(
            factory, owner_actor_id=owner, now=now,
        ) >= 1
        await scan_platform_monitor(factory, owner_actor_id=owner, now=now)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner),
        ) as session:
            incident = await session.scalar(select(PlatformMonitorIncident).where(
                PlatformMonitorIncident.bot_instance_id == bot_id,
                PlatformMonitorIncident.recovered_at.is_(None),
            ))
            assert incident is not None
            events = (
                await session.scalars(select(PlatformMonitorOutbox).where(
                    PlatformMonitorOutbox.incident_id == incident.id,
                ))
            ).all()
            assert len(events) == 1 and events[0].kind == "problem"
            assert incident.last_error_code == "missing_admin_rights"
            assert incident.last_heartbeat == old
            # The shared CI database may contain registered bots from earlier
            # tests. Keep their unrelated alert attempts out of this case.
            other_events = (
                await session.scalars(
                    select(PlatformMonitorOutbox).where(
                        PlatformMonitorOutbox.incident_id != incident.id,
                    )
                )
            ).all()
            for unrelated in other_events:
                unrelated.available_at = now + timedelta(days=1)

        telegram = SimpleNamespace(send_message=AsyncMock())
        assert await deliver_platform_monitor_event(
            factory, bot=telegram, owner_actor_id=owner,
            owner_telegram_id=owner_tg, timezone="Europe/Moscow", now=now,
        )
        assert telegram.send_message.await_count == 1
        kwargs = telegram.send_message.await_args.kwargs
        keyboard = kwargs["reply_markup"].inline_keyboard
        assert keyboard[0][0].callback_data == "plat:bots:30:0"

        # The existing Supervisor writes healthy heartbeat; no second probe.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            health = await session.get(CustomerBotRuntimeHealth, project)
            health.state = "running"
            health.error_code = None
            health.last_heartbeat = now + timedelta(minutes=1)
        assert await scan_platform_monitor(
            factory, owner_actor_id=owner, now=now + timedelta(minutes=1),
        ) == 0
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            health = await session.get(CustomerBotRuntimeHealth, project)
            health.last_heartbeat = now + timedelta(minutes=4)
        assert await scan_platform_monitor(
            factory, owner_actor_id=owner, now=now + timedelta(minutes=4),
        ) >= 1
        assert await deliver_platform_monitor_event(
            factory, bot=telegram, owner_actor_id=owner,
            owner_telegram_id=owner_tg, timezone="Europe/Moscow",
            now=now + timedelta(minutes=4),
        )
        assert telegram.send_message.await_count == 2
        assert "восстановлена" in telegram.send_message.await_args.args[1]
        assert await scan_platform_monitor(
            factory, owner_actor_id=owner, now=now + timedelta(minutes=4),
        ) == 0
        assert await deliver_platform_monitor_event(
            factory, bot=telegram, owner_actor_id=owner,
            owner_telegram_id=owner_tg, timezone="Europe/Moscow",
            now=now + timedelta(minutes=4),
        ) is False
        assert telegram.send_message.await_count == 2

        # Simulate a crashed central delivery after claiming a SECOND
        # incident, then a healthy bot before the expired lease is reclaimed.
        # Never send an obsolete red problem after the recovery is known.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            health = await session.get(CustomerBotRuntimeHealth, project)
            health.state = "error"
            health.error_code = "startup_failed"
            health.last_heartbeat = now + timedelta(minutes=5)
        await scan_platform_monitor(
            factory, owner_actor_id=owner, now=now + timedelta(minutes=5),
        )
        await scan_platform_monitor(
            factory, owner_actor_id=owner, now=now + timedelta(minutes=16),
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner),
        ) as session:
            second = await session.scalar(select(PlatformMonitorIncident).where(
                PlatformMonitorIncident.bot_instance_id == bot_id,
                PlatformMonitorIncident.recovered_at.is_(None),
            ))
            assert second is not None
            abandoned = await session.scalar(select(PlatformMonitorOutbox).where(
                PlatformMonitorOutbox.incident_id == second.id,
                PlatformMonitorOutbox.kind == "problem",
            ))
            assert abandoned is not None
            abandoned.status = "sending"
            abandoned.attempts = 1
            abandoned.claimed_at = now + timedelta(minutes=16)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            health = await session.get(CustomerBotRuntimeHealth, project)
            health.state = "running"
            health.error_code = None
            health.last_heartbeat = now + timedelta(minutes=17)
        await scan_platform_monitor(
            factory, owner_actor_id=owner, now=now + timedelta(minutes=17),
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            health = await session.get(CustomerBotRuntimeHealth, project)
            health.last_heartbeat = now + timedelta(minutes=23)
        assert await scan_platform_monitor(
            factory, owner_actor_id=owner, now=now + timedelta(minutes=23),
        ) >= 1
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner),
        ) as session:
            old_claim = await session.get(PlatformMonitorOutbox, abandoned.id)
            assert old_claim.status == "cancelled"
            # Other CI bots can become stale during our artificial +23-minute
            # time jump and enqueue their own incidents. Keep those outside
            # the case so the next send is this episode's recovery.
            other_outbox = (
                await session.scalars(select(PlatformMonitorOutbox).where(
                    PlatformMonitorOutbox.incident_id != second.id,
                    PlatformMonitorOutbox.status == "pending",
                ))
            ).all()
            for unrelated in other_outbox:
                unrelated.available_at = now + timedelta(days=1)
        assert await deliver_platform_monitor_event(
            factory, bot=telegram, owner_actor_id=owner,
            owner_telegram_id=owner_tg, timezone="Europe/Moscow",
            now=now + timedelta(minutes=23),
        )
        assert "восстановлена" in telegram.send_message.await_args.args[1]
        assert telegram.send_message.await_count == 3
    finally:
        async with admin_engine.begin() as admin:
            await admin.execute(
                text("DELETE FROM platform_operators WHERE telegram_user_id = :id"),
                {"id": owner_tg},
            )
        await engine.dispose()
        await admin_engine.dispose()
