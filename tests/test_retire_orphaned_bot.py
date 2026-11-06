"""Orphaned Telegram BotFather deletion: RLS, no fake access and safe shutdown."""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.access.models import (
    AccessAction,
    AccessState,
    EffectiveAccess,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.deletion_service import (
    count_retirement_blockers,
    request_bot_retirement,
    retire_unreachable_bot_with_orphan_actions,
)
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth
from telegram_membership_saas.modules.owner_setup.connection_management import (
    get_connection_disposition,
)
from telegram_membership_saas.modules.owner_setup.obligation_audit import OwnerObligationAudit
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)


def _tg() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _fixture(factory, *, status="pending", state_actual="unknown",
                   state_desired=False, health="error",
                   locked_at=None, has_rights=False):
    owner = await ensure_telegram_actor(
        factory, telegram_id=_tg(), username="orphan_owner", first_name="Owner",
    )
    outsider = await ensure_telegram_actor(
        factory, telegram_id=_tg(), username="orphan_other", first_name="Other",
    )
    project = await create_owner_project(
        factory, actor_id=owner, name="BotFather-deleted bot",
    )
    unrelated = await create_owner_project(
        factory, actor_id=outsider, name="Other tenant",
    )
    instance_id, action_id = uuid4(), uuid4()
    moment = datetime.now(UTC)
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner, project_id=project),
    ) as session:
        resource = Resource(
            project_id=project, resource_type=ResourceType.TELEGRAM_CHANNEL.value,
            external_id=str(-_tg()), name="Orphan testing channel",
        )
        session.add(resource)
        await session.flush()
        customer = await get_or_create_project_customer(
            session, project_id=project, user_id=outsider,
        )
        session.add(BotInstance(
            id=instance_id, project_id=project,
            telegram_bot_id=_tg(), username="botfather_deleted_test",
            display_name="Gone bot",
            token_ciphertext="encrypted-test-placeholder", status="registered",
        ))
        await session.flush()
        bot_id = await session.scalar(
            select(BotInstance.telegram_bot_id).where(BotInstance.id == instance_id)
        )
        session.add(CustomerBotRuntimeHealth(
            project_id=project, telegram_bot_id=bot_id,
            instance_id=instance_id, state=health,
            last_heartbeat=moment, error_code=(
                "telegram_check_failed" if health == "error" else None
            ),
        ))
        session.add(EffectiveAccess(
            project_id=project, customer_id=customer.id,
            resource_id=resource.id, has_access=has_rights,
            active_grant_count=1 if has_rights else 0,
            revision=1, computed_at=moment,
        ))
        await session.flush()
        session.add(AccessState(
            project_id=project, customer_id=customer.id,
            resource_id=resource.id,
            desired_access=state_desired, desired_revision=1,
            actual_state=state_actual,
        ))
        await session.flush()
        session.add(AccessAction(
            id=action_id, project_id=project, customer_id=customer.id,
            resource_id=resource.id, desired_revision=1,
            action_type="revoke" if not state_desired else "grant",
            dedupe_key="orphan:" + uuid4().hex,
            status=status, locked_at=locked_at,
            available_at=moment - timedelta(minutes=1),
        ))
    return owner, outsider, project, unrelated, instance_id, action_id


async def test_deleted_bot_no_other_obligations_can_retire_and_history_stays():
    engine = build_engine(Settings(_env_file=None))
    factory = build_session_factory(engine)
    try:
        owner, other, project, unrelated, instance, action = await _fixture(factory)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            blocked = await count_retirement_blockers(session, project_id=project)
            assert blocked.access_actions == 1
            assert not any((
                blocked.requests, blocked.subscriptions, blocked.grants,
                blocked.access, blocked.join_tickets,
            ))
        with pytest.raises(ValueError, match="незавершённых действий доступа"):
            await request_bot_retirement(
                factory, actor_id=owner, project_id=project,
                bot_instance_id=instance,
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await retire_unreachable_bot_with_orphan_actions(
                factory, actor_id=other, project_id=project,
                bot_instance_id=instance,
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await retire_unreachable_bot_with_orphan_actions(
                factory, actor_id=owner, project_id=unrelated,
                bot_instance_id=instance,
            )

        assert await retire_unreachable_bot_with_orphan_actions(
            factory, actor_id=owner, project_id=project,
            bot_instance_id=instance,
        ) == 1
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            bot = await session.get(BotInstance, instance)
            command = await session.get(AccessAction, action)
            state = await session.scalar(select(AccessState).where(
                AccessState.project_id == project,
            ))
            assert bot.status == "deleting"
            assert command.status == "superseded"
            assert command.locked_at is None
            assert command.processed_at is not None
            assert state.actual_state == "unknown"  # no fake Telegram revocation
            assert state.desired_access is False
            assert not (await count_retirement_blockers(
                session, project_id=project,
            )).has_blockers
            audit = (await session.scalars(select(OwnerObligationAudit).where(
                OwnerObligationAudit.project_id == project,
            ))).all()
            assert len(audit) == 1
            assert audit[0].actor_id == owner
            assert audit[0].target_id == action
            assert audit[0].action == "abandon_orphan_access_action"
        assert (await get_connection_disposition(
            factory, actor_id=owner, project_id=project,
        )).has_history
        with pytest.raises(ValueError, match="удаляется"):
            await retire_unreachable_bot_with_orphan_actions(
                factory, actor_id=owner, project_id=project,
                bot_instance_id=instance,
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("opts", "expected"),
    [
        ({"state_actual": "granted"}, "неотозванный доступ"),
        ({"state_desired": True, "has_rights": True}, "обязательства"),
        ({"health": "running"}, "работает"),
        ({"status": "processing", "locked_at": datetime.now(UTC)}, "обрабатывается"),
    ],
)
async def test_emergency_removal_stays_locked_for_active_claims(opts, expected):
    engine = build_engine(Settings(_env_file=None))
    factory = build_session_factory(engine)
    try:
        owner, _, project, _, bot, action = await _fixture(factory, **opts)
        with pytest.raises(ValueError, match=expected):
            await retire_unreachable_bot_with_orphan_actions(
                factory, actor_id=owner, project_id=project,
                bot_instance_id=bot,
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            bot_row = await session.get(BotInstance, bot)
            action_row = await session.get(AccessAction, action)
            assert bot_row.status == "registered"
            assert action_row.status in ("pending", "processing")
            assert not (await session.scalars(select(OwnerObligationAudit).where(
                OwnerObligationAudit.project_id == project,
            ))).all()
    finally:
        await engine.dispose()


async def test_stale_processing_action_is_audited_not_marked_succeeded():
    engine = build_engine(Settings(_env_file=None))
    factory = build_session_factory(engine)
    try:
        owner, _, project, _, bot, action = await _fixture(
            factory, status="processing",
            locked_at=datetime.now(UTC) - timedelta(minutes=7),
        )
        assert await retire_unreachable_bot_with_orphan_actions(
            factory, actor_id=owner, project_id=project,
            bot_instance_id=bot,
        ) == 1
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            row = await session.get(AccessAction, action)
            assert row.status == "superseded"
            assert row.processed_at is not None
    finally:
        await engine.dispose()
