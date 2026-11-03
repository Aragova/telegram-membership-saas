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
    AccessState,
    ActualAccessState,
    claim_next_access_action,
    mark_access_action_succeeded,
    reconcile_access_target,
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
    reason="TMS_DATABASE_URL is required for access runtime integration tests",
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


async def _create_project(session_factory, owner_id: UUID) -> UUID:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id),
    ) as session:
        project = Project(owner_user_id=owner_id, name="Uncertain side effect")
        session.add(project)
        await session.flush()
        return project.id


async def test_expired_uncertain_grant_forces_corrective_revoke() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, f"uncertain_owner_{uuid4().hex[:8]}")
        buyer_id = await _create_user(session_factory, f"uncertain_buyer_{uuid4().hex[:8]}")
        project_id = await _create_project(session_factory, owner_id)

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
                name="Uncertain channel",
            )
            session.add(resource)
            await session.flush()
            customer_id = customer.id
            resource_id = resource.id

            initial = await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base,
            )
            assert initial.queued_action is not None
            assert initial.queued_action.action_type == AccessActionType.REVOKE.value

            initial_revoke = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base,
            )
            assert initial_revoke is not None
            await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=initial_revoke.id,
                claim_attempt=initial_revoke.attempt_count,
                processed_at=base,
            )

            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.customer_id == customer_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.actual_state == ActualAccessState.REVOKED.value

            grant = EntitlementGrant(
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                source=EntitlementSource.MANUAL.value,
                source_id=uuid4(),
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=base,
                valid_until=base + timedelta(hours=1),
            )
            session.add(grant)
            await session.flush()

            desired_grant = await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base + timedelta(seconds=1),
            )
            assert desired_grant.queued_action is not None
            assert desired_grant.queued_action.action_type == AccessActionType.GRANT.value

            in_flight_grant = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base + timedelta(seconds=1),
            )
            assert in_flight_grant is not None
            assert in_flight_grant.action_type == AccessActionType.GRANT.value
            grant_action_id = in_flight_grant.id

            # Simulate: external GRANT succeeded, but worker died before recording success.
            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = base + timedelta(seconds=2)
            flipped = await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base + timedelta(seconds=2),
            )
            assert flipped.access_state.desired_access is False
            assert flipped.access_state.actual_state == ActualAccessState.REVOKED.value
            assert flipped.queued_action is None

        reclaim_at = base + timedelta(minutes=6)
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            corrective = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=reclaim_at,
                lease_timeout=timedelta(minutes=5),
            )
            assert corrective is not None
            assert corrective.action_type == AccessActionType.REVOKE.value
            assert corrective.status == AccessActionStatus.PROCESSING.value
            assert corrective.attempt_count == 1

            stale_grant = await session.scalar(
                select(AccessAction).where(AccessAction.id == grant_action_id)
            )
            assert stale_grant is not None
            assert stale_grant.status == AccessActionStatus.SUPERSEDED.value

            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.customer_id == customer_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.actual_state == ActualAccessState.UNKNOWN.value

            await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=corrective.id,
                claim_attempt=corrective.attempt_count,
                processed_at=reclaim_at + timedelta(seconds=1),
            )
            assert state.actual_state == ActualAccessState.REVOKED.value
    finally:
        await engine.dispose()
