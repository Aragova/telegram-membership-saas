from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.access.models import (
    AccessAction,
    AccessActionStatus,
    AccessActionType,
    AccessState,
    ActualAccessState,
    EffectiveAccess,
)
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
)


@dataclass(frozen=True, slots=True)
class AccessRecalculationResult:
    effective_access: EffectiveAccess
    access_state: AccessState
    queued_action: AccessAction | None


def utcnow() -> datetime:
    return datetime.now(UTC)


def require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Access timestamps must be timezone-aware")


async def _ensure_target_rows(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    moment: datetime,
) -> None:
    await session.execute(
        insert(EffectiveAccess)
        .values(
            project_id=project_id,
            customer_id=customer_id,
            resource_id=resource_id,
            has_access=False,
            active_grant_count=0,
            revision=0,
            computed_at=moment,
        )
        .on_conflict_do_nothing(
            index_elements=["project_id", "customer_id", "resource_id"]
        )
    )
    await session.execute(
        insert(AccessState)
        .values(
            project_id=project_id,
            customer_id=customer_id,
            resource_id=resource_id,
            desired_access=False,
            actual_state=ActualAccessState.UNKNOWN.value,
        )
        .on_conflict_do_nothing(
            index_elements=["project_id", "customer_id", "resource_id"]
        )
    )


async def _load_target_for_update(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
) -> tuple[EffectiveAccess, AccessState]:
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
    state = await session.scalar(
        select(AccessState)
        .where(
            AccessState.project_id == project_id,
            AccessState.customer_id == customer_id,
            AccessState.resource_id == resource_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if effective is None or state is None:
        raise RuntimeError("Access target rows could not be resolved")
    return effective, state


async def _load_active_grant_windows(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
) -> tuple[tuple[datetime, datetime | None], ...]:
    rows = (
        await session.execute(
            select(EntitlementGrant.valid_from, EntitlementGrant.valid_until).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.customer_id == customer_id,
                EntitlementGrant.resource_id == resource_id,
                EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
            )
        )
    ).all()
    return tuple((row.valid_from, row.valid_until) for row in rows)


def _evaluate_grants(
    windows: tuple[tuple[datetime, datetime | None], ...],
    *,
    moment: datetime,
) -> tuple[int, datetime | None]:
    active_count = 0
    boundaries: list[datetime] = []

    for valid_from, valid_until in windows:
        if valid_from <= moment and (valid_until is None or valid_until > moment):
            active_count += 1
        if valid_from > moment:
            boundaries.append(valid_from)
        if valid_until is not None and valid_until > moment:
            boundaries.append(valid_until)

    return active_count, min(boundaries) if boundaries else None


async def _find_existing_action(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    action_type: AccessActionType,
) -> AccessAction | None:
    return await session.scalar(
        select(AccessAction)
        .where(
            AccessAction.project_id == project_id,
            AccessAction.customer_id == customer_id,
            AccessAction.resource_id == resource_id,
            AccessAction.action_type == action_type.value,
            AccessAction.status.in_(
                (
                    AccessActionStatus.PENDING.value,
                    AccessActionStatus.PROCESSING.value,
                    AccessActionStatus.FAILED_RETRYABLE.value,
                )
            ),
        )
        .order_by(AccessAction.created_at)
    )


async def recalculate_effective_access(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    calculated_at: datetime | None = None,
) -> AccessRecalculationResult:
    moment = calculated_at or utcnow()
    require_aware(moment)

    await _ensure_target_rows(
        session,
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        moment=moment,
    )
    effective, state = await _load_target_for_update(
        session,
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
    )

    windows = await _load_active_grant_windows(
        session,
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
    )
    active_count, next_change_at = _evaluate_grants(windows, moment=moment)
    has_access = active_count > 0

    effective.has_access = has_access
    effective.active_grant_count = active_count
    effective.next_change_at = next_change_at
    effective.computed_at = moment
    effective.revision += 1

    state.desired_access = has_access

    actual_matches = (
        state.actual_state == ActualAccessState.GRANTED.value
        if has_access
        else state.actual_state == ActualAccessState.REVOKED.value
    )
    queued_action = None

    if not actual_matches:
        action_type = AccessActionType.GRANT if has_access else AccessActionType.REVOKE
        queued_action = await _find_existing_action(
            session,
            project_id=project_id,
            customer_id=customer_id,
            resource_id=resource_id,
            action_type=action_type,
        )
        if queued_action is None:
            queued_action = AccessAction(
                project_id=project_id,
                customer_id=customer_id,
                resource_id=resource_id,
                projection_revision=effective.revision,
                action_type=action_type.value,
                dedupe_key=(
                    f"access:{customer_id}:{resource_id}:"
                    f"r{effective.revision}:{action_type.value}"
                ),
                status=AccessActionStatus.PENDING.value,
                available_at=moment,
            )
            session.add(queued_action)

    await session.flush()
    return AccessRecalculationResult(
        effective_access=effective,
        access_state=state,
        queued_action=queued_action,
    )


async def claim_next_access_action(
    session: AsyncSession,
    *,
    project_id: UUID,
    claimed_at: datetime | None = None,
    lease_timeout: timedelta | None = None,
) -> AccessAction | None:
    moment = claimed_at or utcnow()
    require_aware(moment)
    lease = lease_timeout or timedelta(minutes=5)
    if lease.total_seconds() <= 0:
        raise ValueError("lease_timeout must be positive")
    stale_before = moment - lease
    examined_ids: list[UUID] = []

    for _ in range(20):
        statement = select(AccessAction).where(
            AccessAction.project_id == project_id,
            (
                (
                    AccessAction.status.in_(
                        (
                            AccessActionStatus.PENDING.value,
                            AccessActionStatus.FAILED_RETRYABLE.value,
                        )
                    )
                    & (AccessAction.available_at <= moment)
                )
                | (
                    (AccessAction.status == AccessActionStatus.PROCESSING.value)
                    & (AccessAction.locked_at.is_not(None))
                    & (AccessAction.locked_at <= stale_before)
                )
            ),
        )
        if examined_ids:
            statement = statement.where(AccessAction.id.notin_(examined_ids))

        action = await session.scalar(
            statement.order_by(AccessAction.available_at, AccessAction.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if action is None:
            return None
        examined_ids.append(action.id)

        state = await session.scalar(
            select(AccessState)
            .where(
                AccessState.project_id == project_id,
                AccessState.customer_id == action.customer_id,
                AccessState.resource_id == action.resource_id,
            )
            .with_for_update()
        )
        if state is None:
            raise RuntimeError("Access state disappeared before action claim")

        expected_action = (
            AccessActionType.GRANT.value
            if state.desired_access
            else AccessActionType.REVOKE.value
        )
        if action.action_type != expected_action:
            action.status = AccessActionStatus.SUPERSEDED.value
            action.processed_at = moment
            action.locked_at = None
            action.last_error = None
            await session.flush()
            continue

        other_processing = await session.scalar(
            select(AccessAction.id).where(
                AccessAction.project_id == project_id,
                AccessAction.customer_id == action.customer_id,
                AccessAction.resource_id == action.resource_id,
                AccessAction.status == AccessActionStatus.PROCESSING.value,
                AccessAction.id != action.id,
            )
        )
        if other_processing is not None:
            continue

        action.status = AccessActionStatus.PROCESSING.value
        action.locked_at = moment
        action.processed_at = None
        action.attempt_count += 1
        await session.flush()
        return action

    return None


async def mark_access_action_succeeded(
    session: AsyncSession,
    *,
    project_id: UUID,
    action_id: UUID,
    processed_at: datetime | None = None,
) -> AccessAction:
    moment = processed_at or utcnow()
    require_aware(moment)

    action = await session.scalar(
        select(AccessAction)
        .where(
            AccessAction.id == action_id,
            AccessAction.project_id == project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if action is None:
        raise ValueError("Access action is not accessible in the current project scope")
    if action.status == AccessActionStatus.SUCCEEDED.value:
        return action
    if action.status != AccessActionStatus.PROCESSING.value:
        raise ValueError("Only a processing access action can succeed")

    state = await session.scalar(
        select(AccessState)
        .where(
            AccessState.project_id == project_id,
            AccessState.customer_id == action.customer_id,
            AccessState.resource_id == action.resource_id,
        )
        .with_for_update()
    )
    if state is None:
        raise RuntimeError("Access state disappeared before action completion")

    action.status = AccessActionStatus.SUCCEEDED.value
    action.processed_at = moment
    action.locked_at = None
    action.last_error = None

    state.actual_state = (
        ActualAccessState.GRANTED.value
        if action.action_type == AccessActionType.GRANT.value
        else ActualAccessState.REVOKED.value
    )
    state.last_synced_at = moment
    state.last_error = None

    await session.flush()
    return action


async def mark_access_action_failed(
    session: AsyncSession,
    *,
    project_id: UUID,
    action_id: UUID,
    error: str,
    failed_at: datetime | None = None,
    retry_at: datetime | None = None,
    permanent: bool = False,
) -> AccessAction:
    moment = failed_at or utcnow()
    require_aware(moment)
    if not error.strip():
        raise ValueError("Access action failure must include an error")
    if not permanent:
        if retry_at is None:
            raise ValueError("retry_at is required for retryable access failures")
        require_aware(retry_at)
        if retry_at < moment:
            raise ValueError("retry_at cannot precede failed_at")

    action = await session.scalar(
        select(AccessAction)
        .where(
            AccessAction.id == action_id,
            AccessAction.project_id == project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if action is None:
        raise ValueError("Access action is not accessible in the current project scope")
    if action.status != AccessActionStatus.PROCESSING.value:
        raise ValueError("Only a processing access action can fail")

    state = await session.scalar(
        select(AccessState)
        .where(
            AccessState.project_id == project_id,
            AccessState.customer_id == action.customer_id,
            AccessState.resource_id == action.resource_id,
        )
        .with_for_update()
    )
    if state is None:
        raise RuntimeError("Access state disappeared before action failure")

    action.status = (
        AccessActionStatus.FAILED_PERMANENT.value
        if permanent
        else AccessActionStatus.FAILED_RETRYABLE.value
    )
    action.locked_at = None
    action.last_error = error[:1000]
    if permanent:
        action.processed_at = moment
    else:
        action.processed_at = None
        action.available_at = retry_at

    state.last_error = error[:1000]
    await session.flush()
    return action


async def enqueue_due_access_recalculations(
    session: AsyncSession,
    *,
    project_id: UUID,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    from telegram_membership_saas.modules.outbox import (
        OutboxEvent,
        OutboxEventStatus,
        OutboxEventType,
    )

    moment = now or utcnow()
    require_aware(moment)
    if limit <= 0:
        raise ValueError("limit must be positive")

    due = tuple(
        (
            await session.scalars(
                select(EffectiveAccess)
                .where(
                    EffectiveAccess.project_id == project_id,
                    EffectiveAccess.next_change_at.is_not(None),
                    EffectiveAccess.next_change_at <= moment,
                )
                .order_by(EffectiveAccess.next_change_at, EffectiveAccess.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )

    created = 0
    for effective in due:
        boundary = effective.next_change_at
        if boundary is None:
            continue
        result = await session.execute(
            insert(OutboxEvent)
            .values(
                project_id=project_id,
                event_type=OutboxEventType.ACCESS_RECALCULATE.value,
                aggregate_type="effective_access",
                aggregate_id=effective.id,
                dedupe_key=f"effective_access:{effective.id}:boundary:{boundary.isoformat()}",
                payload={
                    "customer_id": str(effective.customer_id),
                    "resource_id": str(effective.resource_id),
                },
                status=OutboxEventStatus.PENDING.value,
                available_at=moment,
            )
            .on_conflict_do_nothing(index_elements=["project_id", "dedupe_key"])
            .returning(OutboxEvent.id)
        )
        if result.scalar_one_or_none() is not None:
            created += 1

    await session.flush()
    return created
