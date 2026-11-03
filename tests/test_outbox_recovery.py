import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    OutboxEventStatus,
    OutboxEventType,
    claim_next_outbox_event,
    mark_outbox_event_failed,
    process_claimed_access_recalculate_event,
)
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for outbox recovery tests",
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


async def test_processing_outbox_event_is_reclaimed_and_fenced_after_worker_crash() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 6, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, "outbox_owner")
        buyer_id = await _create_user(session_factory, "outbox_buyer")
        project_id = await _create_project(session_factory, owner_id, "Outbox recovery")

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
                external_id=f"-100-outbox-{uuid4()}",
                name="Outbox recovery channel",
                status=ResourceStatus.ACTIVE.value,
            )
            session.add(resource)
            await session.flush()

            event = OutboxEvent(
                project_id=project_id,
                event_type=OutboxEventType.ACCESS_RECALCULATE.value,
                aggregate_type="test",
                aggregate_id=uuid4(),
                dedupe_key=f"outbox-recovery:{uuid4()}",
                payload={
                    "customer_id": str(customer.id),
                    "resource_id": str(resource.id),
                },
                status=OutboxEventStatus.PENDING.value,
                available_at=base,
            )
            session.add(event)
            await session.flush()
            event_id = event.id

            first_claim = await claim_next_outbox_event(
                session,
                project_id=project_id,
                claimed_at=base,
            )
            assert first_claim is not None
            assert first_claim.id == event_id
            assert first_claim.attempt_count == 1
            first_attempt = first_claim.attempt_count

        reclaim_at = base + timedelta(minutes=6)
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            second_claim = await claim_next_outbox_event(
                session,
                project_id=project_id,
                claimed_at=reclaim_at,
                lease_timeout=timedelta(minutes=5),
            )
            assert second_claim is not None
            assert second_claim.id == event_id
            assert second_claim.attempt_count == 2
            second_attempt = second_claim.attempt_count

        with pytest.raises(ValueError, match="lease is no longer current"):
            async with tenant_session_scope(
                session_factory,
                TenantContext(user_id=owner_id, project_id=project_id),
            ) as session:
                stale = OutboxEvent(id=event_id, project_id=project_id)
                await process_claimed_access_recalculate_event(
                    session,
                    event=stale,
                    claim_attempt=first_attempt,
                    processed_at=reclaim_at + timedelta(seconds=1),
                )

        with pytest.raises(ValueError, match="lease is no longer current"):
            async with tenant_session_scope(
                session_factory,
                TenantContext(user_id=owner_id, project_id=project_id),
            ) as session:
                await mark_outbox_event_failed(
                    session,
                    project_id=project_id,
                    event_id=event_id,
                    claim_attempt=first_attempt,
                    error="stale worker failure",
                    failed_at=reclaim_at + timedelta(seconds=1),
                    permanent=True,
                )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            current = OutboxEvent(id=event_id, project_id=project_id)
            processed = await process_claimed_access_recalculate_event(
                session,
                event=current,
                claim_attempt=second_attempt,
                processed_at=reclaim_at + timedelta(seconds=2),
            )
            assert processed.status == OutboxEventStatus.SUCCEEDED.value
            assert processed.attempt_count == second_attempt
    finally:
        await engine.dispose()
