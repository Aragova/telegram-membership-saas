from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.access.models import EffectiveAccess
from telegram_membership_saas.modules.entitlements import EntitlementGrant, EntitlementGrantStatus
from telegram_membership_saas.modules.outbox import OutboxEvent, OutboxEventStatus, OutboxEventType


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def recompute_effective_access(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    as_of: datetime | None = None,
    cause_event_id: UUID | None = None,
) -> EffectiveAccess:
    moment = as_of or _utcnow()
    has_active_grant = (
        await session.scalar(
            select(EntitlementGrant.id)
            .where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.customer_id == customer_id,
                EntitlementGrant.resource_id == resource_id,
                EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
                EntitlementGrant.valid_from <= moment,
                or_(
                    EntitlementGrant.valid_until.is_(None),
                    EntitlementGrant.valid_until > moment,
                ),
            )
            .limit(1)
        )
        is not None
    )

    projection = await session.scalar(
        select(EffectiveAccess)
        .where(
            EffectiveAccess.project_id == project_id,
            EffectiveAccess.customer_id == customer_id,
            EffectiveAccess.resource_id == resource_id,
        )
        .with_for_update()
    )
    changed = projection is None or projection.desired_access != has_active_grant
    if projection is None:
        projection = EffectiveAccess(
            project_id=project_id,
            customer_id=customer_id,
            resource_id=resource_id,
            desired_access=has_active_grant,
        )
        session.add(projection)
    else:
        projection.desired_access = has_active_grant

    await session.flush()

    if changed and cause_event_id is not None:
        session.add(
            OutboxEvent(
                project_id=project_id,
                event_type=OutboxEventType.ACCESS_SYNC.value,
                aggregate_type="effective_access",
                aggregate_id=projection.id,
                dedupe_key=f"access-sync:{cause_event_id}",
                payload={
                    "customer_id": str(customer_id),
                    "resource_id": str(resource_id),
                    "desired_access": "true" if has_active_grant else "false",
                },
                status=OutboxEventStatus.PENDING.value,
            )
        )
        await session.flush()

    return projection


async def process_access_recalculate_event(
    session: AsyncSession,
    *,
    project_id: UUID,
    event_id: UUID,
    as_of: datetime | None = None,
) -> EffectiveAccess:
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
    if event.status == OutboxEventStatus.SUCCEEDED.value:
        projection = await session.scalar(
            select(EffectiveAccess).where(
                EffectiveAccess.project_id == project_id,
                EffectiveAccess.customer_id == UUID(event.payload["customer_id"]),
                EffectiveAccess.resource_id == UUID(event.payload["resource_id"]),
            )
        )
        if projection is None:
            raise RuntimeError("Succeeded recalculate event has no effective access projection")
        return projection
    if event.event_type != OutboxEventType.ACCESS_RECALCULATE.value:
        raise ValueError("Outbox event is not an access recalculation event")
    if event.status not in {
        OutboxEventStatus.PENDING.value,
        OutboxEventStatus.FAILED_RETRYABLE.value,
    }:
        raise ValueError("Outbox event cannot be processed from its current state")

    customer_id = UUID(event.payload["customer_id"])
    resource_id = UUID(event.payload["resource_id"])
    moment = as_of or _utcnow()
    event.status = OutboxEventStatus.PROCESSING.value
    event.locked_at = moment
    event.attempt_count += 1
    await session.flush()

    projection = await recompute_effective_access(
        session,
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        as_of=moment,
        cause_event_id=event.id,
    )
    event.status = OutboxEventStatus.SUCCEEDED.value
    event.processed_at = moment
    event.locked_at = None
    event.last_error = None
    await session.flush()
    return projection
