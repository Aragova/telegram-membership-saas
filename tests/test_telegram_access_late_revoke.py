import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.access import (
    AccessActionStatus,
    AccessActionType,
    AccessState,
    ActualAccessState,
    reconcile_access_target,
)
from telegram_membership_saas.modules.entitlements import EntitlementGrant, EntitlementGrantStatus
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.telegram_access import (
    TelegramAccessResult,
    ensure_owner_service_access,
    run_one_telegram_access_action,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for Telegram access integration tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_user(session_factory) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            user = TelegramUser(
                telegram_user_id=_random_telegram_id(),
                username=f"late_revoke_{uuid4().hex[:8]}",
            )
            session.add(user)
            await session.flush()
            return user.id


async def _create_project_and_resource(session_factory, owner_id: UUID) -> tuple[UUID, UUID]:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id),
    ) as session:
        project = Project(owner_user_id=owner_id, name="Late revoke")
        session.add(project)
        await session.flush()
        project_id = project.id

    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id, project_id=project_id),
    ) as session:
        resource = Resource(
            project_id=project_id,
            resource_type=ResourceType.TELEGRAM_CHANNEL.value,
            external_id=f"-100{uuid4().int % 10_000_000_000}",
            name="Late revoke channel",
        )
        session.add(resource)
        await session.flush()
        return project_id, resource.id


class AlreadyMemberThenRevokeAdapter:
    invite_ttl = timedelta(minutes=15)

    def __init__(self) -> None:
        self.revoke_calls = 0
        self.discard_calls = 0

    async def prepare(self, command):
        if command.access.action_type == AccessActionType.REVOKE:
            self.revoke_calls += 1
            return TelegramAccessResult()
        return TelegramAccessResult(
            resolved_chat_id=int(command.chat_id),
            already_member=True,
        )

    async def deliver(self, command, result):
        return None

    async def discard(self, result):
        self.discard_calls += 1


async def test_late_revoke_records_external_effect_after_lease_expiry() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 17, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory)
        project_id, resource_id = await _create_project_and_resource(session_factory, owner_id)

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            owner_access = await ensure_owner_service_access(
                session,
                project_id=project_id,
                resource_id=resource_id,
                granted_at=base,
            )
            grant_id = owner_access.grant.id

        adapter = AlreadyMemberThenRevokeAdapter()
        granted = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=base,
            clock=lambda: base,
        )
        assert granted is not None
        assert granted.status == AccessActionStatus.SUCCEEDED.value

        revoke_at = base + timedelta(minutes=1)
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            grant = await session.get(EntitlementGrant, grant_id)
            assert grant is not None
            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = revoke_at
            await session.flush()
            runtime = await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=grant.customer_id,
                resource_id=resource_id,
                calculated_at=revoke_at,
            )
            assert runtime.access_state.desired_access is False
            assert runtime.queued_action is not None
            assert runtime.queued_action.action_type == AccessActionType.REVOKE.value

        late_completion = revoke_at + timedelta(seconds=2)
        revoked = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=revoke_at,
            lease_timeout=timedelta(seconds=1),
            clock=lambda: late_completion,
        )
        assert revoked is not None
        assert revoked.status == AccessActionStatus.SUCCEEDED.value
        assert adapter.revoke_calls == 1
        assert adapter.discard_calls == 0

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.desired_access is False
            assert state.actual_state == ActualAccessState.REVOKED.value
    finally:
        await engine.dispose()
