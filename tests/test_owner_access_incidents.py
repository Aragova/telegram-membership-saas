"""Access incident alerts use persisted action failures, never transient API noise."""
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.access.models import (
    AccessAction,
    AccessState,
    EffectiveAccess,
)
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.owner_access_incidents import (
    _is_confirmed_incident,
    _is_resolved,
    prepare_owner_access_notification,
    scan_owner_access_incidents,
)
from telegram_membership_saas.modules.owner_notifications import (
    OwnerNotificationDelivery,
    deliver_one_owner_notification,
    set_owner_setting,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext


def _stamp():
    return datetime.now(UTC)


def _tid():
    return uuid4().int % 7_000_000_000_000_000_000 + 1


@pytest.mark.parametrize(
    ("status", "attempts", "minutes", "expected"),
    [
        ("failed_permanent", 1, 1, True),
        ("failed_retryable", 1, 60, False),
        ("failed_retryable", 3, 9, True),
        ("failed_retryable", 3, 12, True),
        ("processing", 3, 12, True),
        ("succeeded", 3, 12, False),
    ],
)
def test_only_confirmed_failures_trigger(status, attempts, minutes, expected):
    now = _stamp()
    action = SimpleNamespace(
        status=status, attempt_count=attempts,
        created_at=now - timedelta(minutes=minutes), desired_revision=2,
        last_error="Telegram delivery failed",
    )
    state = SimpleNamespace(
        desired_access=True, actual_state="unknown", desired_revision=2,
    )
    assert _is_confirmed_incident(action, state, now=now) is expected
    state.actual_state = "granted"
    assert _is_resolved(state)
    assert not _is_confirmed_incident(action, state, now=now)
    state.actual_state = "unknown"
    state.desired_revision = 3
    assert not _is_confirmed_incident(action, state, now=now)


@pytest.mark.skipif(not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL")
async def test_access_incident_and_recovery_once_with_owner_scope():
    engine = build_engine(Settings(_env_file=None))
    factory = build_session_factory(engine)
    now = _stamp()
    owner_tid, buyer_tid = _tid(), _tid()
    owner = await ensure_telegram_actor(
        factory, telegram_id=owner_tid, username="incident_owner",
        first_name="Owner",
    )
    buyer = await ensure_telegram_actor(
        factory, telegram_id=buyer_tid, username="incident_buyer",
        first_name="Buyer",
    )
    project = await create_owner_project(
        factory, actor_id=owner, name="Access incident test",
    )
    try:
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            customer = ProjectCustomer(project_id=project, user_id=buyer)
            session.add(customer)
            await session.flush()
            resource = Resource(
                project_id=project, resource_type="telegram_channel",
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="<Premium & Channel>",
            )
            session.add(resource)
            await session.flush()
            effective = EffectiveAccess(
                project_id=project, customer_id=customer.id,
                resource_id=resource.id, has_access=True,
                active_grant_count=1, revision=1, computed_at=now,
            )
            session.add(effective)
            await session.flush()
            state = AccessState(
                project_id=project, customer_id=customer.id,
                resource_id=resource.id, desired_access=True,
                desired_revision=1, actual_state="unknown",
            )
            session.add(state)
            await session.flush()
            action = AccessAction(
                project_id=project, customer_id=customer.id,
                resource_id=resource.id, desired_revision=1,
                action_type="grant", dedupe_key=f"incident:{uuid4()}",
                status="failed_permanent", attempt_count=1,
                last_error="TOKEN=never expose credentials",
                available_at=now,
            )
            session.add(action)
            await session.flush()
            action_id = action.id
            customer_id = customer.id

        assert await scan_owner_access_incidents(
            factory, project_id=project, now=now + timedelta(seconds=2),
        ) == 1
        assert await scan_owner_access_incidents(
            factory, project_id=project, now=now + timedelta(seconds=3),
        ) == 0  # handled event excluded by SQL before the scan limit
        async with system_project_session_scope(factory, project) as session:
            alerts = (await session.scalars(select(OwnerNotificationDelivery).where(
                OwnerNotificationDelivery.project_id == project,
            ))).all()
            assert len(alerts) == 1
            assert alerts[0].telegram_id == owner_tid
            assert alerts[0].kind == "access"
            assert alerts[0].event_key == str(action_id)

        bot = SimpleNamespace(send_message=AsyncMock())
        assert await deliver_one_owner_notification(
            factory, project_id=project, bot=bot, now=now + timedelta(seconds=4),
        )
        assert not await deliver_one_owner_notification(
            factory, project_id=project, bot=bot, now=now + timedelta(seconds=5),
        )
        first = bot.send_message.await_args.args[1]
        assert "Проблема с доступом" in first
        assert "incident_buyer" in first
        assert "&lt;Premium &amp; Channel&gt;" in first
        assert "TOKEN" not in first
        buttons = [
            b.callback_data for row in
            bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert len(buttons) == 1
        assert buttons[0].startswith("own:subscriber:")
        assert len(buttons[0]) <= 64

        # The original grant never succeeded. An expired subscription that
        # changes DESIRED access to revoked must NOT be called recovery.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            state = await session.scalar(select(AccessState).where(
                AccessState.project_id == project,
                AccessState.customer_id == customer_id,
            ))
            state.desired_access = False
            state.desired_revision = 2
            state.actual_state = "revoked"
        assert await scan_owner_access_incidents(
            factory, project_id=project, now=now + timedelta(seconds=30),
        ) == 0

        # Only a direction-matched successful Telegram action after the
        # original owner alert is a verified recovery.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            state = await session.scalar(select(AccessState).where(
                AccessState.project_id == project,
                AccessState.customer_id == customer_id,
            ))
            state.desired_access = True
            state.desired_revision = 3
            state.actual_state = "granted"
            session.add(AccessAction(
                project_id=project, customer_id=customer_id,
                resource_id=resource.id, desired_revision=3,
                action_type="grant", dedupe_key=f"recovery:{uuid4()}",
                status="succeeded", attempt_count=1,
                processed_at=now + timedelta(minutes=1),
                available_at=now,
            ))

        assert await scan_owner_access_incidents(
            factory, project_id=project, now=now + timedelta(minutes=1),
        ) == 1
        assert await scan_owner_access_incidents(
            factory, project_id=project, now=now + timedelta(minutes=2),
        ) == 0
        assert await deliver_one_owner_notification(
            factory, project_id=project, bot=bot,
            now=now + timedelta(minutes=3),
        )
        assert bot.send_message.await_count == 2
        second = bot.send_message.await_args.args[1]
        assert "Доступ восстановлен" in second
        assert "TOKEN" not in second

        # New failures can be muted by owner, without mutating access.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            await set_owner_setting(
                session, project_id=project, actor_id=owner,
                category="access", enabled=False,
            )
            state = await session.scalar(select(AccessState).where(
                AccessState.project_id == project,
                AccessState.customer_id == customer_id,
            ))
            state.actual_state = "unknown"
        assert await scan_owner_access_incidents(
            factory, project_id=project, now=now + timedelta(minutes=4),
        ) == 0

        # Recipients other than the project owner are never authorized.
        assert await prepare_owner_access_notification(
            factory, project_id=project, recipient=buyer_tid,
            payload={"action_id": action_id.hex}, recovered=False,
            now=now + timedelta(minutes=5),
        ) is None
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("language", "heading", "recovery"),
    [
        ("ru", "Проблема с доступом", "Доступ восстановлен"),
        ("en", "Access problem detected", "Access issue resolved"),
    ],
)
def test_access_problem_templates_are_localized(language, heading, recovery):
    token = _language.set(language)
    try:
        from telegram_membership_saas.localization import tr

        assert heading in tr(
            "🚨 <b>Проблема с доступом</b>",
            "🚨 <b>Access problem detected</b>",
        )
        assert recovery in tr(
            "✅ <b>Доступ восстановлен</b>",
            "✅ <b>Access issue resolved</b>",
        )
    finally:
        _language.reset(token)
