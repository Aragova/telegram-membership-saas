from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.access.models import EffectiveAccess
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Access timestamps must be timezone-aware")


async def recalculate_effective_access(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    calculated_at: datetime | None = None,
) -> EffectiveAccess:
    if calculated_at is not None:
        require_aware(calculated_at)

    provisional_moment = calculated_at or utcnow()
    await session.execute(
        insert(EffectiveAccess)
        .values(
            project_id=project_id,
            customer_id=customer_id,
            resource_id=resource_id,
            has_access=False,
            active_grant_count=0,
            revision=0,
            computed_at=provisional_moment,
        )
        .on_conflict_do_nothing(
            index_elements=["project_id", "customer_id", "resource_id"]
        )
    )

    effective = await session.scalar(
        select(EffectiveAccess)
        .where(
            EffectiveAccess.project_id == project_id,
            EffectiveAccess.customer_id == customer_id,
            EffectiveAccess.resource_id == resource_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if effective is None:
        raise RuntimeError("Effective access row could not be resolved")

    moment = calculated_at or utcnow()
    require_aware(moment)
    if (
        calculated_at is not None
        and effective.revision > 0
        and calculated_at < effective.computed_at
    ):
        raise ValueError("calculated_at cannot precede existing computed_at")

    grant_windows = (
        await session.execute(
            select(EntitlementGrant.valid_from, EntitlementGrant.valid_until).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.customer_id == customer_id,
                EntitlementGrant.resource_id == resource_id,
                EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
            )
        )
    ).all()

    active_count = 0
    boundaries: list[datetime] = []
    for valid_from, valid_until in grant_windows:
        if valid_from <= moment and (valid_until is None or valid_until > moment):
            active_count += 1
        if valid_from > moment:
            boundaries.append(valid_from)
        if valid_until is not None and valid_until > moment:
            boundaries.append(valid_until)

    effective.active_grant_count = active_count
    effective.has_access = active_count > 0
    effective.next_change_at = min(boundaries) if boundaries else None
    effective.computed_at = moment
    effective.revision += 1
    await session.flush()
    return effective
