from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.access import recalculate_effective_access
from telegram_membership_saas.modules.outbox.models import (
    OutboxEvent,
    OutboxEventStatus,
    OutboxEventType,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Outbox timestamps must be timezone-aware")


async def claim_next_outbox_event(
    session: AsyncSession,
    *,
    project_id: UUID,
    claimed_at: datetime | None = None,
) -> OutboxEvent | None:
    moment = claimed_at or utcnow()
    require_aware(moment)

    event = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.project_id == project_id,
            OutboxEvent.status.in_(
                (
                    OutboxEventStatus.PENDING.value,
                    OutboxEventStatus.FAILED_RETRYABLE.value,
                )
            ),
            OutboxEvent.available_at <= moment,
        )
        .order_by(OutboxEvent.available_at, OutboxEvent.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if event is None:
        return None

    event.status = OutboxEventStatus.PROCESSING.value
    event.locked_at = moment
    event.attempt_count += 1
    await session.flush()
    return event


async def process_claimed_access_recalculate_event(
    session: AsyncSession,
    *,
    event: OutboxEvent,
    processed_at: datetime | None = None,
) -> OutboxEvent:
    moment = processed_at or utcnow()
    require_aware(moment)

    if event.status == OutboxEventStatus.SUCCEEDED.value:
        return event
    if event.status != OutboxEventStatus.PROCESSING.value:
        raise ValueError("Only a processing outbox event can be handled")
    if event.event_type != OutboxEventType.ACCESS_RECALCULATE.value:
        raise ValueError("Unsupported outbox event type")

    try:
        customer_id = UUID(event.payload["customer_id"])
        resource_id = UUID(event.payload["resource_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ACCESS_RECALCULATE payload is invalid") from exc

    await recalculate_effective_access(
        session,
        project_id=event.project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        calculated_at=moment,
    )

    event.status = OutboxEventStatus.SUCCEEDED.value
    event.processed_at = moment
    event.locked_at = None
    event.last_error = None
    await session.flush()
    return event


async def mark_outbox_event_failed(
    session: AsyncSession,
    *,
    project_id: UUID,
    event_id: UUID,
    error: str,
    failed_at: datetime | None = None,
    permanent: bool = False,
    retry_delay: timedelta | None = None,
) -> OutboxEvent:
    moment = failed_at or utcnow()
    require_aware(moment)
    if not error.strip():
        raise ValueError("Outbox failure must include an error")
    delay = retry_delay or timedelta(seconds=30)
    if delay.total_seconds() < 0:
        raise ValueError("retry_delay cannot be negative")

    event = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.id == event_id,
            OutboxEvent.project_id == project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if event is None:
        raise ValueError("Outbox event is not accessible in the current project scope")
    if event.status != OutboxEventStatus.PROCESSING.value:
        raise ValueError("Only a processing outbox event can fail")

    event.status = (
        OutboxEventStatus.FAILED_PERMANENT.value
        if permanent
        else OutboxEventStatus.FAILED_RETRYABLE.value
    )
    event.locked_at = None
    event.last_error = error[:1000]
    if permanent:
        event.processed_at = moment
    else:
        event.available_at = moment + delay

    await session.flush()
    return event
