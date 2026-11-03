import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.access import (
    AccessState,
    ActualAccessState,
    EffectiveAccess,
    recalculate_effective_access,
    sync_access_state_from_projection,
)
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for access-state integration tests",
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


async def test_access_state_tracks_desired_without_overwriting_actual_and_rls() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 14, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, f"state_owner_{uuid4().hex[:8]}")
        buyer_id = await _create_user(session_factory, f"state_buyer_{uuid4().hex[:8]}")
        project_a = await _create_project(session_factory, owner_id, "Access state A")
        project_b = await _create_project(session_factory, owner_id, "Access state B")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_a),
        ) as session:
            customer = await get_or_create_project_customer(
                session,
                project_id=project_a,
                user_id=buyer_id,
            )
            resource = Resource(
                project_id=project_a,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="Access state channel",
            )
            session.add(resource)
            await session.flush()

            grant = EntitlementGrant(
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                source=EntitlementSource.MANUAL.value,
                source_id=uuid4(),
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=base - timedelta(hours=1),
                valid_until=base + timedelta(hours=1),
            )
            session.add(grant)
            await session.flush()

            effective = await recalculate_effective_access(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base,
            )
            assert effective.has_access is True
            assert effective.revision == 1

            state = await sync_access_state_from_projection(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
            )
            assert state.desired_access is True
            assert state.desired_revision == 1
            assert state.actual_state == ActualAccessState.UNKNOWN.value

            state.actual_state = ActualAccessState.GRANTED.value
            state.last_synced_at = base
            await session.flush()

            revoked_at = base + timedelta(minutes=1)
            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = revoked_at
            await session.flush()

            effective = await recalculate_effective_access(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=revoked_at,
            )
            assert effective.has_access is False
            assert effective.revision == 2

            state = await sync_access_state_from_projection(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
            )
            assert state.desired_access is False
            assert state.desired_revision == 2
            assert state.actual_state == ActualAccessState.GRANTED.value
            assert state.last_synced_at == base

            duplicate = await sync_access_state_from_projection(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
            )
            assert duplicate.id == state.id
            assert duplicate.desired_revision == 2

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_b),
        ) as session:
            assert list((await session.scalars(select(EffectiveAccess))).all()) == []
            assert list((await session.scalars(select(AccessState))).all()) == []
    finally:
        await engine.dispose()
