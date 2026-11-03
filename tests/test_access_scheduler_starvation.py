import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.access import (
    EffectiveAccess,
    enqueue_due_access_recalculations,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    OutboxEventStatus,
    OutboxEventType,
)
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for access scheduler tests",
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


async def test_scheduler_pages_past_existing_boundary_events_before_limit() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 8, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, "scheduler_owner")
        buyer_id = await _create_user(session_factory, "scheduler_buyer")
        project_id = await _create_project(session_factory, owner_id, "Scheduler starvation")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            customer = await get_or_create_project_customer(
                session,
                project_id=project_id,
                user_id=buyer_id,
            )

            projections: list[EffectiveAccess] = []
            for index in range(3):
                resource = Resource(
                    project_id=project_id,
                    resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                    external_id=f"-100-scheduler-{uuid4()}",
                    name=f"Scheduler channel {index}",
                    status=ResourceStatus.ACTIVE.value,
                )
                session.add(resource)
                await session.flush()

                projection = EffectiveAccess(
                    project_id=project_id,
                    customer_id=customer.id,
                    resource_id=resource.id,
                    has_access=False,
                    active_grant_count=0,
                    revision=1,
                    next_change_at=base - timedelta(minutes=3 - index),
                    computed_at=base - timedelta(minutes=10),
                )
                session.add(projection)
                await session.flush()
                projections.append(projection)

            # The first two due targets already have durable boundary events. A scheduler
            # that applies LIMIT before skipping these conflicts will keep selecting them
            # and starve the third target forever.
            for projection in projections[:2]:
                boundary = projection.next_change_at
                assert boundary is not None
                session.add(
                    OutboxEvent(
                        project_id=project_id,
                        event_type=OutboxEventType.ACCESS_RECALCULATE.value,
                        aggregate_type="effective_access",
                        aggregate_id=projection.id,
                        dedupe_key=(
                            f"effective_access:{projection.id}:boundary:{boundary.isoformat()}"
                        ),
                        payload={
                            "customer_id": str(projection.customer_id),
                            "resource_id": str(projection.resource_id),
                        },
                        status=OutboxEventStatus.FAILED_PERMANENT.value,
                        available_at=base - timedelta(minutes=1),
                        processed_at=base - timedelta(seconds=30),
                        last_error="permanent test failure",
                    )
                )
            await session.flush()

            created = await enqueue_due_access_recalculations(
                session,
                project_id=project_id,
                now=base,
                limit=1,
            )
            assert created == 1

            third_boundary = projections[2].next_change_at
            assert third_boundary is not None
            expected_key = (
                f"effective_access:{projections[2].id}:boundary:{third_boundary.isoformat()}"
            )
            created_event = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.project_id == project_id,
                    OutboxEvent.dedupe_key == expected_key,
                )
            )
            assert created_event is not None
            assert created_event.aggregate_id == projections[2].id
            assert created_event.status == OutboxEventStatus.PENDING.value
    finally:
        await engine.dispose()
