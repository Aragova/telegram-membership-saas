import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.access import EffectiveAccess, recalculate_effective_access
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
    reason="TMS_DATABASE_URL is required for effective access integration tests",
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


async def test_projection_boundaries_overlap_and_rls() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, f"projection_owner_{uuid4().hex[:8]}")
        buyer_id = await _create_user(session_factory, f"projection_buyer_{uuid4().hex[:8]}")
        project_a = await _create_project(session_factory, owner_id, "Projection A")
        project_b = await _create_project(session_factory, owner_id, "Projection B")

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
                name="Projection channel",
            )
            session.add(resource)
            await session.flush()

            grants = [
                EntitlementGrant(
                    project_id=project_a,
                    customer_id=customer.id,
                    resource_id=resource.id,
                    source=EntitlementSource.MANUAL.value,
                    source_id=uuid4(),
                    status=EntitlementGrantStatus.ACTIVE.value,
                    valid_from=base - timedelta(hours=1),
                    valid_until=base + timedelta(hours=1),
                ),
                EntitlementGrant(
                    project_id=project_a,
                    customer_id=customer.id,
                    resource_id=resource.id,
                    source=EntitlementSource.MANUAL.value,
                    source_id=uuid4(),
                    status=EntitlementGrantStatus.ACTIVE.value,
                    valid_from=base - timedelta(minutes=30),
                    valid_until=base + timedelta(minutes=30),
                ),
                EntitlementGrant(
                    project_id=project_a,
                    customer_id=customer.id,
                    resource_id=resource.id,
                    source=EntitlementSource.MANUAL.value,
                    source_id=uuid4(),
                    status=EntitlementGrantStatus.ACTIVE.value,
                    valid_from=base + timedelta(hours=1),
                    valid_until=base + timedelta(hours=2),
                ),
            ]
            session.add_all(grants)
            await session.flush()

            initial = await recalculate_effective_access(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base,
            )
            assert initial.has_access is True
            assert initial.active_grant_count == 2
            assert initial.next_change_at == base + timedelta(minutes=30)
            assert initial.revision == 1

            after_overlap = await recalculate_effective_access(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base + timedelta(minutes=30),
            )
            assert after_overlap.has_access is True
            assert after_overlap.active_grant_count == 1
            assert after_overlap.next_change_at == base + timedelta(hours=1)
            assert after_overlap.revision == 2

            at_renewal_boundary = await recalculate_effective_access(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base + timedelta(hours=1),
            )
            assert at_renewal_boundary.has_access is True
            assert at_renewal_boundary.active_grant_count == 1
            assert at_renewal_boundary.next_change_at == base + timedelta(hours=2)

            expired = await recalculate_effective_access(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base + timedelta(hours=2),
            )
            assert expired.has_access is False
            assert expired.active_grant_count == 0
            assert expired.next_change_at is None

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_b),
        ) as session:
            assert list((await session.scalars(select(EffectiveAccess))).all()) == []
    finally:
        await engine.dispose()


async def test_projection_rejects_naive_timestamp() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        owner_id = await _create_user(session_factory, f"naive_owner_{uuid4().hex[:8]}")
        buyer_id = await _create_user(session_factory, f"naive_buyer_{uuid4().hex[:8]}")
        project_id = await _create_project(session_factory, owner_id, "Naive")

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
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="Naive timestamp channel",
            )
            session.add(resource)
            await session.flush()

            with pytest.raises(ValueError, match="timezone-aware"):
                await recalculate_effective_access(
                    session,
                    project_id=project_id,
                    customer_id=customer.id,
                    resource_id=resource.id,
                    calculated_at=datetime(2026, 11, 3, 12, 0),
                )
    finally:
        await engine.dispose()


async def test_projection_rejects_stale_explicit_recalculation() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    stale_at = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    fresh_at = stale_at + timedelta(hours=2)

    try:
        owner_id = await _create_user(session_factory, f"stale_owner_{uuid4().hex[:8]}")
        buyer_id = await _create_user(session_factory, f"stale_buyer_{uuid4().hex[:8]}")
        project_id = await _create_project(session_factory, owner_id, "Stale guard")

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
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="Stale guard channel",
            )
            session.add(resource)
            await session.flush()
            session.add(
                EntitlementGrant(
                    project_id=project_id,
                    customer_id=customer.id,
                    resource_id=resource.id,
                    source=EntitlementSource.MANUAL.value,
                    source_id=uuid4(),
                    status=EntitlementGrantStatus.ACTIVE.value,
                    valid_from=stale_at - timedelta(hours=1),
                    valid_until=stale_at + timedelta(hours=1),
                )
            )
            await session.flush()

            fresh = await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=fresh_at,
            )
            assert fresh.has_access is False
            assert fresh.computed_at == fresh_at
            assert fresh.revision == 1

            with pytest.raises(ValueError, match="cannot precede existing computed_at"):
                await recalculate_effective_access(
                    session,
                    project_id=project_id,
                    customer_id=customer.id,
                    resource_id=resource.id,
                    calculated_at=stale_at,
                )

            projection = await session.scalar(
                select(EffectiveAccess).where(
                    EffectiveAccess.project_id == project_id,
                    EffectiveAccess.customer_id == customer.id,
                    EffectiveAccess.resource_id == resource.id,
                )
            )
            assert projection is not None
            assert projection.has_access is False
            assert projection.computed_at == fresh_at
            assert projection.revision == 1
    finally:
        await engine.dispose()
