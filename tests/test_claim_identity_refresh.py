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
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    OutboxEventStatus,
    OutboxEventType,
    claim_next_outbox_event,
)
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for identity-map reclaim tests",
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
            external_id=f"-100-refresh-{uuid4()}",
            name="Identity refresh channel",
            status=ResourceStatus.ACTIVE.value,
        )
        session.add(resource)
        await session.flush()
        return customer.id, resource.id


async def _seed_action_and_event(
    session,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    base: datetime,
) -> tuple[UUID, UUID]:
    state = AccessState(
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        desired_access=True,
        actual_state=ActualAccessState.REVOKED.value,
    )
    action = AccessAction(
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        projection_revision=1,
        action_type=AccessActionType.GRANT.value,
        dedupe_key=f"identity-refresh-action:{uuid4()}",
        status=AccessActionStatus.PENDING.value,
        available_at=base,
    )
    event = OutboxEvent(
        project_id=project_id,
        event_type=OutboxEventType.ACCESS_RECALCULATE.value,
        aggregate_type="identity_refresh_test",
        aggregate_id=uuid4(),
        dedupe_key=f"identity-refresh-event:{uuid4()}",
        payload={
            "customer_id": str(customer_id),
            "resource_id": str(resource_id),
        },
        status=OutboxEventStatus.PENDING.value,
        available_at=base,
    )
    session.add_all((state, action, event))
    await session.flush()
    return action.id, event.id


async def test_reused_session_refreshes_attempt_generation_before_reclaim() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 9, 12, 0, tzinfo=UTC)
    lease = timedelta(minutes=5)

    try:
        owner_id = await _create_user(session_factory, "refresh_generation_owner")
        buyer_id = await _create_user(session_factory, "refresh_generation_buyer")
        project_id = await _create_project(session_factory, owner_id, "Refresh generation")
        customer_id, resource_id = await _create_target(
            session_factory,
            owner_id,
            buyer_id,
            project_id,
        )
        context = TenantContext(user_id=owner_id, project_id=project_id)

        async with session_factory() as session_a:
            async with session_a.begin():
                await apply_tenant_context(session_a, context)
                action_id, event_id = await _seed_action_and_event(
                    session_a,
                    project_id=project_id,
                    customer_id=customer_id,
                    resource_id=resource_id,
                    base=base,
                )
                first_action = await claim_next_access_action(
                    session_a,
                    project_id=project_id,
                    claimed_at=base,
                    lease_timeout=lease,
                )
                first_event = await claim_next_outbox_event(
                    session_a,
                    project_id=project_id,
                    claimed_at=base,
                    lease_timeout=lease,
                )
                assert first_action is not None and first_action.id == action_id
                assert first_event is not None and first_event.id == event_id
                assert first_action.attempt_count == 1
                assert first_event.attempt_count == 1

            second_at = base + timedelta(minutes=6)
            async with tenant_session_scope(session_factory, context) as session_b:
                second_action = await claim_next_access_action(
                    session_b,
                    project_id=project_id,
                    claimed_at=second_at,
                    lease_timeout=lease,
                )
                second_event = await claim_next_outbox_event(
                    session_b,
                    project_id=project_id,
                    claimed_at=second_at,
                    lease_timeout=lease,
                )
                assert second_action is not None and second_action.id == action_id
                assert second_event is not None and second_event.id == event_id
                assert second_action.attempt_count == 2
                assert second_event.attempt_count == 2

            # session_a still holds attempt-1 ORM identities because expire_on_commit=False.
            # The claim queries must refresh them from PostgreSQL before incrementing again.
            third_at = base + timedelta(minutes=12)
            async with session_a.begin():
                await apply_tenant_context(session_a, context)
                third_action = await claim_next_access_action(
                    session_a,
                    project_id=project_id,
                    claimed_at=third_at,
                    lease_timeout=lease,
                )
                third_event = await claim_next_outbox_event(
                    session_a,
                    project_id=project_id,
                    claimed_at=third_at,
                    lease_timeout=lease,
                )
                assert third_action is not None and third_action.id == action_id
                assert third_event is not None and third_event.id == event_id
                assert third_action.attempt_count == 3
                assert third_event.attempt_count == 3
    finally:
        await engine.dispose()


async def test_reused_session_refreshes_desired_state_before_claim() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
    lease = timedelta(minutes=5)

    try:
        owner_id = await _create_user(session_factory, "refresh_desired_owner")
        buyer_id = await _create_user(session_factory, "refresh_desired_buyer")
        project_id = await _create_project(session_factory, owner_id, "Refresh desired state")
        customer_id, resource_id = await _create_target(
            session_factory,
            owner_id,
            buyer_id,
            project_id,
        )
        context = TenantContext(user_id=owner_id, project_id=project_id)

        async with session_factory() as session_a:
            async with session_a.begin():
                await apply_tenant_context(session_a, context)
                action_id, _ = await _seed_action_and_event(
                    session_a,
                    project_id=project_id,
                    customer_id=customer_id,
                    resource_id=resource_id,
                    base=base,
                )
                first_action = await claim_next_access_action(
                    session_a,
                    project_id=project_id,
                    claimed_at=base,
                    lease_timeout=lease,
                )
                assert first_action is not None
                cached_state = await session_a.scalar(
                    select(AccessState).where(
                        AccessState.project_id == project_id,
                        AccessState.customer_id == customer_id,
                        AccessState.resource_id == resource_id,
                    )
                )
                assert cached_state is not None
                assert cached_state.desired_access is True

            async with tenant_session_scope(session_factory, context) as session_b:
                fresh_state = await session_b.scalar(
                    select(AccessState)
                    .where(
                        AccessState.project_id == project_id,
                        AccessState.customer_id == customer_id,
                        AccessState.resource_id == resource_id,
                    )
                    .with_for_update()
                )
                assert fresh_state is not None
                fresh_state.desired_access = False
                await session_b.flush()

            async with session_a.begin():
                await apply_tenant_context(session_a, context)
                reclaimed = await claim_next_access_action(
                    session_a,
                    project_id=project_id,
                    claimed_at=base + timedelta(minutes=6),
                    lease_timeout=lease,
                )
                # The cached desired=True must be refreshed to False, making the old GRANT stale.
                assert reclaimed is None
                refreshed_action = await session_a.scalar(
                    select(AccessAction)
                    .where(
                        AccessAction.id == action_id,
                        AccessAction.project_id == project_id,
                    )
                    .execution_options(populate_existing=True)
                )
                assert refreshed_action is not None
                assert refreshed_action.status == AccessActionStatus.SUPERSEDED.value
    finally:
        await engine.dispose()
