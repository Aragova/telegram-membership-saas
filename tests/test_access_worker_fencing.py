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
    claim_next_access_action,
    mark_access_action_failed,
    mark_access_action_succeeded,
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
    reason="TMS_DATABASE_URL is required for access worker fencing tests",
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


async def _create_target(session_factory, owner_id: UUID, buyer_id: UUID, project_id: UUID):
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
            external_id=f"-100-{uuid4()}",
            name="Worker fencing channel",
            status=ResourceStatus.ACTIVE.value,
        )
        session.add(resource)
        await session.flush()
        return customer.id, resource.id


async def _set_actual_revoked(
    session,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    moment: datetime,
) -> None:
    initial = await recalculate_effective_access(
        session,
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        calculated_at=moment,
    )
    assert initial.queued_action is not None
    assert initial.queued_action.action_type == AccessActionType.REVOKE.value
    action = await claim_next_access_action(
        session,
        project_id=project_id,
        claimed_at=moment,
    )
    assert action is not None
    await mark_access_action_succeeded(
        session,
        project_id=project_id,
        action_id=action.id,
        claim_attempt=action.attempt_count,
        processed_at=moment,
    )


async def test_opposite_processing_action_enqueues_corrective_action() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 3, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, "corrective_owner")
        buyer_id = await _create_user(session_factory, "corrective_buyer")
        project_id = await _create_project(session_factory, owner_id, "Corrective")
        customer_id, resource_id = await _create_target(
            session_factory,
            owner_id,
            buyer_id,
            project_id,
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            await _set_actual_revoked(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                moment=base,
            )

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

            desired_true = await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base + timedelta(seconds=1),
            )
            assert desired_true.queued_action is not None
            assert desired_true.queued_action.action_type == AccessActionType.GRANT.value

            in_flight_grant = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base + timedelta(seconds=1),
            )
            assert in_flight_grant is not None
            assert in_flight_grant.action_type == AccessActionType.GRANT.value
            assert in_flight_grant.status == AccessActionStatus.PROCESSING.value

            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = base + timedelta(seconds=2)
            desired_false = await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base + timedelta(seconds=2),
            )
            assert desired_false.effective_access.has_access is False
            assert desired_false.access_state.actual_state == ActualAccessState.REVOKED.value
            assert desired_false.queued_action is not None
            assert desired_false.queued_action.action_type == AccessActionType.REVOKE.value

            await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=in_flight_grant.id,
                claim_attempt=in_flight_grant.attempt_count,
                processed_at=base + timedelta(seconds=3),
            )

            corrective = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base + timedelta(seconds=3),
            )
            assert corrective is not None
            assert corrective.id == desired_false.queued_action.id
            assert corrective.action_type == AccessActionType.REVOKE.value
            await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=corrective.id,
                claim_attempt=corrective.attempt_count,
                processed_at=base + timedelta(seconds=4),
            )

            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.customer_id == customer_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.desired_access is False
            assert state.actual_state == ActualAccessState.REVOKED.value
    finally:
        await engine.dispose()


async def test_reclaimed_lease_observes_stale_success_but_rejects_stale_failure() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 4, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, "generation_owner")
        buyer_id = await _create_user(session_factory, "generation_buyer")
        project_id = await _create_project(session_factory, owner_id, "Generation")
        customer_id, resource_id = await _create_target(
            session_factory,
            owner_id,
            buyer_id,
            project_id,
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            grant = EntitlementGrant(
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                source=EntitlementSource.MANUAL.value,
                source_id=uuid4(),
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=base - timedelta(minutes=1),
                valid_until=base + timedelta(hours=1),
            )
            session.add(grant)
            await session.flush()
            await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base,
            )
            first_claim = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base,
            )
            assert first_claim is not None
            action_id = first_claim.id
            first_attempt = first_claim.attempt_count
            assert first_attempt == 1

        reclaim_at = base + timedelta(minutes=6)
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            second_claim = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=reclaim_at,
                lease_timeout=timedelta(minutes=5),
            )
            assert second_claim is not None
            assert second_claim.id == action_id
            second_attempt = second_claim.attempt_count
            assert second_attempt == 2

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            observed = await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=action_id,
                claim_attempt=first_attempt,
                processed_at=reclaim_at + timedelta(seconds=1),
            )
            assert observed.status == AccessActionStatus.PROCESSING.value
            assert observed.attempt_count == second_attempt
            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.customer_id == customer_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.actual_state == ActualAccessState.GRANTED.value

        with pytest.raises(ValueError, match="lease is no longer current"):
            async with tenant_session_scope(
                session_factory,
                TenantContext(user_id=owner_id, project_id=project_id),
            ) as session:
                await mark_access_action_failed(
                    session,
                    project_id=project_id,
                    action_id=action_id,
                    claim_attempt=first_attempt,
                    error="stale worker failure",
                    failed_at=reclaim_at + timedelta(seconds=1),
                    permanent=True,
                )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            completed = await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=action_id,
                claim_attempt=second_attempt,
                processed_at=reclaim_at + timedelta(seconds=2),
            )
            assert completed.status == AccessActionStatus.SUCCEEDED.value
            assert completed.attempt_count == second_attempt
    finally:
        await engine.dispose()


async def test_late_success_after_superseded_action_requeues_correction() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 5, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, "late_owner")
        buyer_id = await _create_user(session_factory, "late_buyer")
        project_id = await _create_project(session_factory, owner_id, "Late success")
        customer_id, resource_id = await _create_target(
            session_factory,
            owner_id,
            buyer_id,
            project_id,
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            await _set_actual_revoked(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                moment=base,
            )
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
            await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base + timedelta(seconds=1),
            )
            grant_action = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base + timedelta(seconds=1),
            )
            assert grant_action is not None
            grant_action_id = grant_action.id
            grant_attempt = grant_action.attempt_count

            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = base + timedelta(seconds=2)
            desired_false = await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base + timedelta(seconds=2),
            )
            assert desired_false.queued_action is not None
            assert desired_false.queued_action.action_type == AccessActionType.REVOKE.value

        reclaim_at = base + timedelta(minutes=7)
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
            stale_grant = await session.get(type(corrective), grant_action_id)
            assert stale_grant is not None
            assert stale_grant.status == AccessActionStatus.SUPERSEDED.value
            await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=corrective.id,
                claim_attempt=corrective.attempt_count,
                processed_at=reclaim_at,
            )

        late_at = reclaim_at + timedelta(seconds=1)
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            late = await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=grant_action_id,
                claim_attempt=grant_attempt,
                processed_at=late_at,
            )
            assert late.status == AccessActionStatus.SUPERSEDED.value
            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.customer_id == customer_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.desired_access is False
            assert state.actual_state == ActualAccessState.GRANTED.value

            fresh_correction = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=late_at,
            )
            assert fresh_correction is not None
            assert fresh_correction.action_type == AccessActionType.REVOKE.value
            await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=fresh_correction.id,
                claim_attempt=fresh_correction.attempt_count,
                processed_at=late_at + timedelta(seconds=1),
            )

            final_state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.customer_id == customer_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert final_state is not None
            assert final_state.actual_state == ActualAccessState.REVOKED.value
    finally:
        await engine.dispose()
