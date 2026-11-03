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
    lease_timeout: timedelta | None = None,
) -> OutboxEvent | None:
    moment = claimed_at or utcnow()
    require_aware(moment)
    lease = lease_timeout or timedelta(minutes=5)
    if lease.total_seconds() <= 0:
        raise ValueError("lease_timeout must be positive")
    stale_before = moment - lease

    event = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.project_id == project_id,
            (
                (
                    OutboxEvent.status.in_(
                        (
                            OutboxEventStatus.PENDING.value,
                            OutboxEventStatus.FAILED_RETRYABLE.value,
                        )
                    )
                    & (OutboxEvent.available_at <= moment)
                )
                | (
                    (OutboxEvent.status == OutboxEventStatus.PROCESSING.value)
                    & (OutboxEvent.locked_at.is_not(None))
                    & (OutboxEvent.locked_at <= stale_before)
                )
            ),
        )
        .execution_options(populate_existing=True)
        .order_by(OutboxEvent.available_at, OutboxEvent.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if event is None:
        return None

    event.status = OutboxEventStatus.PROCESSING.value
    event.locked_at = moment
    event.processed_at = None
    event.attempt_count += 1
    await session.flush()
    return event


async def process_claimed_access_recalculate_event(
    session: AsyncSession,
    *,
    event: OutboxEvent,
    claim_attempt: int,
    processed_at: datetime | None = None,
) -> OutboxEvent:
    moment = processed_at or utcnow()
    require_aware(moment)
    if claim_attempt <= 0:
        raise ValueError("claim_attempt must be positive")

    current_event = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.id == event.id,
            OutboxEvent.project_id == event.project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if current_event is None:
        raise ValueError("Outbox event is not accessible in the current project scope")
    if current_event.attempt_count != claim_attempt:
        raise ValueError("Outbox event lease is no longer current")
    if current_event.status == OutboxEventStatus.SUCCEEDED.value:
        return current_event
    if current_event.status != OutboxEventStatus.PROCESSING.value:
        raise ValueError("Only a processing outbox event can be handled")
    if current_event.event_type != OutboxEventType.ACCESS_RECALCULATE.value:
        raise ValueError("Unsupported outbox event type")

    try:
        customer_id = UUID(current_event.payload["customer_id"])
        resource_id = UUID(current_event.payload["resource_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ACCESS_RECALCULATE payload is invalid") from exc

    await recalculate_effective_access(
        session,
        project_id=current_event.project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        calculated_at=moment,
    )

    current_event.status = OutboxEventStatus.SUCCEEDED.value
    current_event.processed_at = moment
    current_event.locked_at = None
    current_event.last_error = None
    await session.flush()
    return current_event


async def mark_outbox_event_failed(
    session: AsyncSession,
    *,
    project_id: UUID,
    event_id: UUID,
    claim_attempt: int,
    error: str,
    failed_at: datetime | None = None,
    permanent: bool = False,
    retry_delay: timedelta | None = None,
) -> OutboxEvent:
    moment = failed_at or utcnow()
    require_aware(moment)
    if claim_attempt <= 0:
        raise ValueError("claim_attempt must be positive")
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
    if event.attempt_count != claim_attempt:
        raise ValueError("Outbox event lease is no longer current")
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
        event.processed_at = None
        event.available_at = moment + delay

    await session.flush()
    return event
