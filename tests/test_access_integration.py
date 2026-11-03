import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.access import (
    AccessAction,
    AccessActionStatus,
    AccessActionType,
    AccessState,
    ActualAccessState,
    EffectiveAccess,
    claim_next_access_action,
    enqueue_due_access_recalculations,
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
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    OutboxEventStatus,
    claim_next_outbox_event,
    process_claimed_access_recalculate_event,
)
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for access integration tests",
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


async def test_effective_access_boundaries_actions_and_rls() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, "access_owner")
        buyer_id = await _create_user(session_factory, "access_buyer")
        project_a = await _create_project(session_factory, owner_id, "Access A")
        project_b = await _create_project(session_factory, owner_id, "Access B")

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
                external_id="-100-access-a",
                name="Access channel A",
                status=ResourceStatus.ACTIVE.value,
            )
            session.add(resource)
            await session.flush()

            first_grant = EntitlementGrant(
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                source=EntitlementSource.MANUAL.value,
                source_id=uuid4(),
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=base - timedelta(hours=1),
                valid_until=base + timedelta(hours=1),
            )
            renewal_grant = EntitlementGrant(
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                source=EntitlementSource.MANUAL.value,
                source_id=uuid4(),
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=base + timedelta(hours=1),
                valid_until=base + timedelta(hours=2),
            )
            session.add_all([first_grant, renewal_grant])
            await session.flush()

            initial = await recalculate_effective_access(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base,
            )
            assert initial.effective_access.has_access is True
            assert initial.effective_access.active_grant_count == 1
            assert initial.effective_access.next_change_at == base + timedelta(hours=1)
            assert initial.queued_action is not None
            assert initial.queued_action.action_type == AccessActionType.GRANT.value

            duplicate = await recalculate_effective_access(
                session,
                project_id=project_a,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base,
            )
            assert duplicate.queued_action is not None
            assert duplicate.queued_action.id == initial.queued_action.id
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AccessAction)
                    .where(AccessAction.project_id == project_a)
                )
            ) == 1

            grant_action = await claim_next_access_action(
                session,
                project_id=project_a,
                claimed_at=base,
            )
            assert grant_action is not None
            assert grant_action.action_type == AccessActionType.GRANT.value
            assert grant_action.status == AccessActionStatus.PROCESSING.value
            await mark_access_action_succeeded(
                session,
                project_id=project_a,
                action_id=grant_action.id,
                claim_attempt=grant_action.attempt_count,
                processed_at=base,
            )

            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_a,
                    AccessState.customer_id == customer.id,
                    AccessState.resource_id == resource.id,
                )
            )
            assert state is not None
            assert state.desired_access is True
            assert state.actual_state == ActualAccessState.GRANTED.value

            assert (
                await enqueue_due_access_recalculations(
                    session,
                    project_id=project_a,
                    now=base + timedelta(hours=1),
                )
            ) == 1
            boundary_event = await claim_next_outbox_event(
                session,
                project_id=project_a,
                claimed_at=base + timedelta(hours=1),
            )
            assert boundary_event is not None
            await process_claimed_access_recalculate_event(
                session,
                event=boundary_event,
                claim_attempt=boundary_event.attempt_count,
                processed_at=base + timedelta(hours=1),
            )
            assert boundary_event.status == OutboxEventStatus.SUCCEEDED.value

            effective = await session.scalar(
                select(EffectiveAccess).where(
                    EffectiveAccess.project_id == project_a,
                    EffectiveAccess.customer_id == customer.id,
                    EffectiveAccess.resource_id == resource.id,
                )
            )
            assert effective is not None
            assert effective.has_access is True
            assert effective.active_grant_count == 1
            assert effective.next_change_at == base + timedelta(hours=2)
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AccessAction)
                    .where(AccessAction.project_id == project_a)
                )
            ) == 1

            assert (
                await enqueue_due_access_recalculations(
                    session,
                    project_id=project_a,
                    now=base + timedelta(hours=2),
                )
            ) == 1
            expiry_event = await claim_next_outbox_event(
                session,
                project_id=project_a,
                claimed_at=base + timedelta(hours=2),
            )
            assert expiry_event is not None
            await process_claimed_access_recalculate_event(
                session,
                event=expiry_event,
                claim_attempt=expiry_event.attempt_count,
                processed_at=base + timedelta(hours=2),
            )

            revoke_action = await claim_next_access_action(
                session,
                project_id=project_a,
                claimed_at=base + timedelta(hours=2),
            )
            assert revoke_action is not None
            assert revoke_action.action_type == AccessActionType.REVOKE.value

            retry_at = base + timedelta(hours=2, minutes=5)
            await mark_access_action_failed(
                session,
                project_id=project_a,
                action_id=revoke_action.id,
                claim_attempt=revoke_action.attempt_count,
                error="temporary adapter outage",
                failed_at=base + timedelta(hours=2),
                retry_at=retry_at,
            )
            assert (
                await claim_next_access_action(
                    session,
                    project_id=project_a,
                    claimed_at=retry_at - timedelta(seconds=1),
                )
            ) is None

            retry_action = await claim_next_access_action(
                session,
                project_id=project_a,
                claimed_at=retry_at,
            )
            assert retry_action is not None
            assert retry_action.id == revoke_action.id
            assert retry_action.attempt_count == 2
            await mark_access_action_succeeded(
                session,
                project_id=project_a,
                action_id=retry_action.id,
                claim_attempt=retry_action.attempt_count,
                processed_at=retry_at,
            )

            final_state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_a,
                    AccessState.customer_id == customer.id,
                    AccessState.resource_id == resource.id,
                )
            )
            assert final_state is not None
            assert final_state.desired_access is False
            assert final_state.actual_state == ActualAccessState.REVOKED.value

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_b),
        ) as session:
            assert list((await session.scalars(select(EffectiveAccess))).all()) == []
            assert list((await session.scalars(select(AccessState))).all()) == []
            assert list((await session.scalars(select(AccessAction))).all()) == []
            assert list((await session.scalars(select(OutboxEvent))).all()) == []
    finally:
        await engine.dispose()


async def test_stale_pending_action_is_superseded_before_external_execution() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, "supersede_owner")
        buyer_id = await _create_user(session_factory, "supersede_buyer")
        project_id = await _create_project(session_factory, owner_id, "Supersede")

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
                external_id="-100-supersede",
                name="Supersede channel",
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

            first = await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base,
            )
            assert first.queued_action is not None
            stale_grant_action_id = first.queued_action.id

            second_moment = base + timedelta(seconds=1)
            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = second_moment
            second = await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=second_moment,
            )
            assert second.effective_access.has_access is False
            assert second.queued_action is not None
            assert second.queued_action.action_type == AccessActionType.REVOKE.value

            claimed = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=second_moment,
            )
            assert claimed is not None
            assert claimed.action_type == AccessActionType.REVOKE.value

            stale = await session.get(AccessAction, stale_grant_action_id)
            assert stale is not None
            assert stale.status == AccessActionStatus.SUPERSEDED.value
    finally:
        await engine.dispose()
