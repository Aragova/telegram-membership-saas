import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.access import (
    AccessAction,
    AccessActionStatus,
    AccessActionType,
    claim_next_access_action,
    recalculate_effective_access,
)
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for access concurrency tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_user(session_factory, username: str) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            user = TelegramUser(telegram_user_id=_random_telegram_id(), username=username)
            session.add(user)
            await session.flush()
            return user.id


async def _create_project(session_factory, owner_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id),
    ) as session:
        project = Project(owner_user_id=owner_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def test_stale_processing_action_is_reclaimed_before_newer_pending_action() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 2, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, "lease_owner")
        buyer_id = await _create_user(session_factory, "lease_buyer")
        project_id = await _create_project(session_factory, owner_id, "Lease reclaim")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            customer = await get_or_create_project_customer(
                session,
                project_id=project_id,
                user_id=buyer_id,
            )
            resource = Resource(
                project_id=project_id,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id="-100-lease-reclaim",
                name="Lease reclaim channel",
                status=ResourceStatus.ACTIVE.value,
            )
            session.add(resource)
            await session.flush()

            grant = EntitlementGrant(
                project_id=project_id,
                customer_id=customer.id,
                resource_id=resource.id,
                source=EntitlementSource.MANUAL.value,
                source_id=uuid4(),
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=base - timedelta(minutes=1),
                valid_until=base + timedelta(hours=1),
            )
            session.add(grant)
            await session.flush()

            recalculated = await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base,
            )
            assert recalculated.queued_action is not None

            first = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base,
            )
            assert first is not None
            assert first.status == AccessActionStatus.PROCESSING.value
            assert first.attempt_count == 1
            first_id = first.id
            customer_id = customer.id
            resource_id = resource.id

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            newer_pending = AccessAction(
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                projection_revision=999,
                action_type=AccessActionType.GRANT.value,
                dedupe_key=f"lease-regression:{uuid4()}",
                status=AccessActionStatus.PENDING.value,
                available_at=base - timedelta(minutes=1),
            )
            session.add(newer_pending)
            await session.flush()
            newer_pending_id = newer_pending.id

        reclaim_at = base + timedelta(minutes=6)
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            reclaimed = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=reclaim_at,
                lease_timeout=timedelta(minutes=5),
            )
            assert reclaimed is not None
            assert reclaimed.id == first_id
            assert reclaimed.status == AccessActionStatus.PROCESSING.value
            assert reclaimed.attempt_count == 2
            assert reclaimed.locked_at == reclaim_at

            pending = await session.scalar(
                select(AccessAction).where(AccessAction.id == newer_pending_id)
            )
            assert pending is not None
            assert pending.status == AccessActionStatus.PENDING.value
    finally:
        await engine.dispose()
