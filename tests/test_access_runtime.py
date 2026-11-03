import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    apply_tenant_context,
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.access import (
    AccessAction,
    AccessActionStatus,
    AccessActionType,
    AccessState,
    ActualAccessState,
    claim_next_access_action,
    enqueue_due_access_recalculations,
    mark_access_action_failed,
    mark_access_action_succeeded,
    reconcile_access_target,
)
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    OutboxEventStatus,
    claim_next_outbox_event,
    mark_outbox_event_failed,
    process_claimed_access_recalculate_event,
)
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


async def _create_project(session_factory, owner_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id),
    ) as session:
        project = Project(owner_user_id=owner_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def _create_target(
    session_factory,
    *,
    owner_id: UUID,
    buyer_id: UUID,
    project_id: UUID,
    name: str,
) -> tuple[UUID, UUID]:
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
            name=name,
        )
        session.add(resource)
        await session.flush()
        return customer.id, resource.id


async def _add_grant(
    session,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    valid_from: datetime,
    valid_until: datetime | None,
) -> EntitlementGrant:
    grant = EntitlementGrant(
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        source=EntitlementSource.MANUAL.value,
        source_id=uuid4(),
        status=EntitlementGrantStatus.ACTIVE.value,
        valid_from=valid_from,
        valid_until=valid_until,
    )
    session.add(grant)
    await session.flush()
    return grant


async def test_runtime_retry_success_and_rls() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, f"runtime_owner_{uuid4().hex[:8]}")
        buyer_id = await _create_user(session_factory, f"runtime_buyer_{uuid4().hex[:8]}")
        project_a = await _create_project(session_factory, owner_id, "Runtime A")
        project_b = await _create_project(session_factory, owner_id, "Runtime B")
        customer_id, resource_id = await _create_target(
            session_factory,
            owner_id=owner_id,
            buyer_id=buyer_id,
            project_id=project_a,
            name="Runtime channel",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_a),
        ) as session:
            await _add_grant(
                session,
                project_id=project_a,
                customer_id=customer_id,
                resource_id=resource_id,
                valid_from=base - timedelta(minutes=1),
                valid_until=base + timedelta(hours=1),
            )
            runtime = await reconcile_access_target(
                session,
                project_id=project_a,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base,
            )
            assert runtime.access_state.desired_access is True
            assert runtime.access_state.actual_state == ActualAccessState.UNKNOWN.value
            assert runtime.queued_action is not None
            assert runtime.queued_action.action_type == AccessActionType.GRANT.value

            claimed = await claim_next_access_action(
                session,
                project_id=project_a,
                claimed_at=base,
            )
            assert claimed is not None
            assert claimed.attempt_count == 1
            await mark_access_action_failed(
                session,
                project_id=project_a,
                action_id=claimed.id,
                claim_attempt=1,
                error="temporary adapter failure",
                failed_at=base,
                retry_at=base + timedelta(seconds=30),
            )
            assert claimed.status == AccessActionStatus.FAILED_RETRYABLE.value

            assert (
                await claim_next_access_action(
                    session,
                    project_id=project_a,
                    claimed_at=base + timedelta(seconds=29),
                )
                is None
            )
            retry = await claim_next_access_action(
                session,
                project_id=project_a,
                claimed_at=base + timedelta(seconds=31),
            )
            assert retry is not None
            assert retry.id == claimed.id
            assert retry.attempt_count == 2

            completed = await mark_access_action_succeeded(
                session,
                project_id=project_a,
                action_id=retry.id,
                claim_attempt=2,
                processed_at=base + timedelta(seconds=32),
            )
            assert completed.status == AccessActionStatus.SUCCEEDED.value
            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_a,
                    AccessState.customer_id == customer_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.actual_state == ActualAccessState.GRANTED.value
            assert state.last_error is None

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_b),
        ) as session:
            assert list((await session.scalars(select(AccessAction))).all()) == []
    finally:
        await engine.dispose()


async def test_late_opposite_success_queues_correction() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 17, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, f"flip_owner_{uuid4().hex[:8]}")
        buyer_id = await _create_user(session_factory, f"flip_buyer_{uuid4().hex[:8]}")
        project_id = await _create_project(session_factory, owner_id, "Flip")
        customer_id, resource_id = await _create_target(
            session_factory,
            owner_id=owner_id,
            buyer_id=buyer_id,
            project_id=project_id,
            name="Flip channel",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            grant = await _add_grant(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                valid_from=base - timedelta(minutes=1),
                valid_until=base + timedelta(hours=1),
            )
            await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base,
            )
            grant_action = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base,
            )
            assert grant_action is not None
            grant_action_id = grant_action.id
            grant_attempt = grant_action.attempt_count

            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = base + timedelta(seconds=1)
            flipped = await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base + timedelta(seconds=1),
            )
            assert flipped.access_state.desired_access is False
            assert flipped.queued_action is not None
            assert flipped.queued_action.action_type == AccessActionType.REVOKE.value

            late = await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=grant_action_id,
                claim_attempt=grant_attempt,
                processed_at=base + timedelta(seconds=2),
            )
            assert late.status == AccessActionStatus.SUCCEEDED.value

            corrective = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base + timedelta(seconds=2),
            )
            assert corrective is not None
            assert corrective.action_type == AccessActionType.REVOKE.value
            await mark_access_action_succeeded(
                session,
                project_id=project_id,
                action_id=corrective.id,
                claim_attempt=corrective.attempt_count,
                processed_at=base + timedelta(seconds=3),
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


async def test_concurrent_claim_has_one_winner() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, f"claim_owner_{uuid4().hex[:8]}")
        buyer_id = await _create_user(session_factory, f"claim_buyer_{uuid4().hex[:8]}")
        project_id = await _create_project(session_factory, owner_id, "Claim")
        customer_id, resource_id = await _create_target(
            session_factory,
            owner_id=owner_id,
            buyer_id=buyer_id,
            project_id=project_id,
            name="Claim channel",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            await _add_grant(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                valid_from=base - timedelta(minutes=1),
                valid_until=base + timedelta(hours=1),
            )
            await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base,
            )

        async def claim_once():
            async with tenant_session_scope(
                session_factory,
                TenantContext(user_id=owner_id, project_id=project_id),
            ) as session:
                return await claim_next_access_action(
                    session,
                    project_id=project_id,
                    claimed_at=base,
                )

        first, second = await asyncio.gather(claim_once(), claim_once())
        winners = [action for action in (first, second) if action is not None]
        assert len(winners) == 1
        assert winners[0].status == AccessActionStatus.PROCESSING.value
        assert winners[0].attempt_count == 1
    finally:
        await engine.dispose()


async def test_same_session_reclaim_refreshes_action_generation() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 19, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, f"refresh_owner_{uuid4().hex[:8]}")
        buyer_id = await _create_user(session_factory, f"refresh_buyer_{uuid4().hex[:8]}")
        project_id = await _create_project(session_factory, owner_id, "Refresh")
        customer_id, resource_id = await _create_target(
            session_factory,
            owner_id=owner_id,
            buyer_id=buyer_id,
            project_id=project_id,
            name="Refresh channel",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            await _add_grant(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                valid_from=base - timedelta(minutes=1),
                valid_until=base + timedelta(hours=1),
            )
            await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                calculated_at=base,
            )

        context = TenantContext(user_id=owner_id, project_id=project_id)
        async with session_factory() as session_a:
            async with session_a.begin():
                await apply_tenant_context(session_a, context)
                first = await claim_next_access_action(
                    session_a,
                    project_id=project_id,
                    claimed_at=base,
                )
                assert first is not None
                assert first.attempt_count == 1
                action_id = first.id

            async with tenant_session_scope(session_factory, context) as session_b:
                second = await claim_next_access_action(
                    session_b,
                    project_id=project_id,
                    claimed_at=base + timedelta(minutes=6),
                    lease_timeout=timedelta(minutes=5),
                )
                assert second is not None
                assert second.id == action_id
                assert second.attempt_count == 2

            async with session_a.begin():
                await apply_tenant_context(session_a, context)
                third = await claim_next_access_action(
                    session_a,
                    project_id=project_id,
                    claimed_at=base + timedelta(minutes=12),
                    lease_timeout=timedelta(minutes=5),
                )
                assert third is not None
                assert third.id == action_id
                assert third.attempt_count == 3
    finally:
        await engine.dispose()


async def test_boundary_scheduler_skips_existing_event_and_recovers_outbox_lease() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    boundary = base + timedelta(hours=1)

    try:
        owner_id = await _create_user(session_factory, f"scheduler_owner_{uuid4().hex[:8]}")
        buyer_a = await _create_user(session_factory, f"scheduler_a_{uuid4().hex[:8]}")
        buyer_b = await _create_user(session_factory, f"scheduler_b_{uuid4().hex[:8]}")
        project_id = await _create_project(session_factory, owner_id, "Scheduler")
        target_a = await _create_target(
            session_factory,
            owner_id=owner_id,
            buyer_id=buyer_a,
            project_id=project_id,
            name="Scheduler A",
        )
        target_b = await _create_target(
            session_factory,
            owner_id=owner_id,
            buyer_id=buyer_b,
            project_id=project_id,
            name="Scheduler B",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            for customer_id, resource_id in (target_a, target_b):
                await _add_grant(
                    session,
                    project_id=project_id,
                    customer_id=customer_id,
                    resource_id=resource_id,
                    valid_from=base - timedelta(minutes=1),
                    valid_until=boundary,
                )
                await reconcile_access_target(
                    session,
                    project_id=project_id,
                    customer_id=customer_id,
                    resource_id=resource_id,
                    calculated_at=base,
                )

            first_created = await enqueue_due_access_recalculations(
                session,
                project_id=project_id,
                now=boundary,
                limit=1,
            )
            second_created = await enqueue_due_access_recalculations(
                session,
                project_id=project_id,
                now=boundary,
                limit=1,
            )
            assert first_created == 1
            assert second_created == 1
            events = list((await session.scalars(select(OutboxEvent))).all())
            assert len(events) == 2
            assert all(event.status == OutboxEventStatus.PENDING.value for event in events)

            claimed = await claim_next_outbox_event(
                session,
                project_id=project_id,
                claimed_at=boundary,
            )
            assert claimed is not None
            event_id = claimed.id
            assert claimed.attempt_count == 1

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            reclaimed = await claim_next_outbox_event(
                session,
                project_id=project_id,
                claimed_at=boundary + timedelta(minutes=6),
                lease_timeout=timedelta(minutes=5),
            )
            assert reclaimed is not None
            assert reclaimed.id == event_id
            assert reclaimed.attempt_count == 2
            processed = await process_claimed_access_recalculate_event(
                session,
                event=reclaimed,
                claim_attempt=2,
                processed_at=boundary + timedelta(minutes=6),
            )
            assert processed.status == OutboxEventStatus.SUCCEEDED.value

            queued = await session.scalar(
                select(AccessAction)
                .where(
                    AccessAction.project_id == project_id,
                    AccessAction.action_type == AccessActionType.REVOKE.value,
                )
                .order_by(AccessAction.created_at)
            )
            assert queued is not None

            another = await claim_next_outbox_event(
                session,
                project_id=project_id,
                claimed_at=boundary + timedelta(minutes=6),
            )
            assert another is not None
            await mark_outbox_event_failed(
                session,
                project_id=project_id,
                event_id=another.id,
                claim_attempt=another.attempt_count,
                error="temporary scheduler consumer failure",
                failed_at=boundary + timedelta(minutes=6),
                retry_delay=timedelta(seconds=30),
            )
            assert another.status == OutboxEventStatus.FAILED_RETRYABLE.value
    finally:
        await engine.dispose()
