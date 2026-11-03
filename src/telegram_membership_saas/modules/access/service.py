from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
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
class AccessRuntimeResult:
    effective_access: EffectiveAccess
    access_state: AccessState
    queued_action: AccessAction | None


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


async def sync_access_state_from_projection(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
) -> AccessState:
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
        raise RuntimeError("Effective access projection must exist before access state")

    await session.execute(
        insert(AccessState)
        .values(
            project_id=project_id,
            customer_id=customer_id,
            resource_id=resource_id,
            desired_access=effective.has_access,
            desired_revision=effective.revision,
            actual_state=ActualAccessState.UNKNOWN.value,
        )
        .on_conflict_do_nothing(
            index_elements=["project_id", "customer_id", "resource_id"]
        )
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
    if state is None:
        raise RuntimeError("Access state row could not be resolved")
    if state.desired_revision > effective.revision:
        raise RuntimeError("Access state revision cannot exceed projection revision")

    state.desired_access = effective.has_access
    state.desired_revision = effective.revision
    await session.flush()
    return state


def _desired_action_type(state: AccessState) -> AccessActionType:
    return AccessActionType.GRANT if state.desired_access else AccessActionType.REVOKE


def _actual_matches_desired(state: AccessState) -> bool:
    expected = (
        ActualAccessState.GRANTED.value
        if state.desired_access
        else ActualAccessState.REVOKED.value
    )
    return state.actual_state == expected


def _action_dedupe_key(state: AccessState, action_type: AccessActionType) -> str:
    observed_at = state.last_synced_at.isoformat() if state.last_synced_at else "never"
    return (
        f"access:{state.customer_id}:{state.resource_id}:"
        f"r{state.desired_revision}:{action_type.value}:"
        f"actual-{state.actual_state}:{observed_at}"
    )


async def enqueue_access_action_for_state(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    available_at: datetime | None = None,
) -> AccessAction | None:
    moment = available_at or utcnow()
    require_aware(moment)

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
    if state is None:
        raise RuntimeError("Access state must exist before an action can be queued")
    if _actual_matches_desired(state):
        return None

    action_type = _desired_action_type(state)
    live_action = await session.scalar(
        select(AccessAction)
        .where(
            AccessAction.project_id == project_id,
            AccessAction.customer_id == customer_id,
            AccessAction.resource_id == resource_id,
            AccessAction.desired_revision == state.desired_revision,
            AccessAction.action_type == action_type.value,
            AccessAction.status.in_(
                (
                    AccessActionStatus.PENDING.value,
                    AccessActionStatus.PROCESSING.value,
                    AccessActionStatus.FAILED_RETRYABLE.value,
                )
            ),
        )
        .order_by(AccessAction.created_at, AccessAction.id)
    )
    if live_action is not None:
        return live_action

    dedupe_key = _action_dedupe_key(state, action_type)
    existing = await session.scalar(
        select(AccessAction).where(
            AccessAction.project_id == project_id,
            AccessAction.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return existing

    action = AccessAction(
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        desired_revision=state.desired_revision,
        action_type=action_type.value,
        dedupe_key=dedupe_key,
        status=AccessActionStatus.PENDING.value,
        available_at=moment,
    )
    session.add(action)
    await session.flush()
    return action


async def reconcile_access_target(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    resource_id: UUID,
    calculated_at: datetime | None = None,
) -> AccessRuntimeResult:
    effective = await recalculate_effective_access(
        session,
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        calculated_at=calculated_at,
    )
    state = await sync_access_state_from_projection(
        session,
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
    )
    action = await enqueue_access_action_for_state(
        session,
        project_id=project_id,
        customer_id=customer_id,
        resource_id=resource_id,
        available_at=effective.computed_at,
    )
    return AccessRuntimeResult(
        effective_access=effective,
        access_state=state,
        queued_action=action,
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

    for _ in range(100):
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
            statement.execution_options(populate_existing=True)
            .order_by(AccessAction.available_at, AccessAction.created_at, AccessAction.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if action is None:
            return None
        examined_ids.append(action.id)

        was_expired_processing = (
            action.status == AccessActionStatus.PROCESSING.value
            and action.locked_at is not None
            and action.locked_at <= stale_before
        )

        state = await session.scalar(
            select(AccessState)
            .where(
                AccessState.project_id == project_id,
                AccessState.customer_id == action.customer_id,
                AccessState.resource_id == action.resource_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if state is None:
            raise RuntimeError("Access state disappeared before action claim")

        expected_action = _desired_action_type(state)
        action_is_stale = (
            action.desired_revision != state.desired_revision
            or action.action_type != expected_action.value
            or _actual_matches_desired(state)
        )
        if action_is_stale:
            action.status = AccessActionStatus.SUPERSEDED.value
            action.processed_at = moment
            action.locked_at = None
            action.last_error = None
            if was_expired_processing:
                # The external call may have succeeded before the worker died. Persisted actual
                # state can no longer be trusted; force a corrective command for current desired.
                state.actual_state = ActualAccessState.UNKNOWN.value
            await session.flush()
            if was_expired_processing:
                await enqueue_access_action_for_state(
                    session,
                    project_id=project_id,
                    customer_id=action.customer_id,
                    resource_id=action.resource_id,
                    available_at=moment,
                )
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
    claim_attempt: int,
    processed_at: datetime | None = None,
) -> AccessAction:
    moment = processed_at or utcnow()
    require_aware(moment)
    if claim_attempt <= 0:
        raise ValueError("claim_attempt must be positive")

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
    if claim_attempt > action.attempt_count:
        raise ValueError("claim_attempt cannot exceed the current action generation")

    state = await session.scalar(
        select(AccessState)
        .where(
            AccessState.project_id == project_id,
            AccessState.customer_id == action.customer_id,
            AccessState.resource_id == action.resource_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if state is None:
        raise RuntimeError("Access state disappeared before action completion")

    state.actual_state = (
        ActualAccessState.GRANTED.value
        if action.action_type == AccessActionType.GRANT.value
        else ActualAccessState.REVOKED.value
    )
    state.last_synced_at = moment
    state.last_error = None

    is_current_claim = (
        action.status == AccessActionStatus.PROCESSING.value
        and action.attempt_count == claim_attempt
    )
    if is_current_claim:
        action.status = AccessActionStatus.SUCCEEDED.value
        action.processed_at = moment
        action.locked_at = None
        action.last_error = None

    await session.flush()
    await enqueue_access_action_for_state(
        session,
        project_id=project_id,
        customer_id=action.customer_id,
        resource_id=action.resource_id,
        available_at=moment,
    )
    return action


async def mark_access_action_failed(
    session: AsyncSession,
    *,
    project_id: UUID,
    action_id: UUID,
    claim_attempt: int,
    error: str,
    failed_at: datetime | None = None,
    retry_at: datetime | None = None,
    permanent: bool = False,
) -> AccessAction:
    moment = failed_at or utcnow()
    require_aware(moment)
    if claim_attempt <= 0:
        raise ValueError("claim_attempt must be positive")
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
    if action.attempt_count != claim_attempt:
        raise ValueError("Access action lease is no longer current")
    if action.status != AccessActionStatus.PROCESSING.value:
        raise ValueError("Only a processing access action can fail")

    state = await session.scalar(
        select(AccessState)
        .where(
            AccessState.project_id == project_id,
            AccessState.customer_id == action.customer_id,
            AccessState.resource_id == action.resource_id,
        )
        .execution_options(populate_existing=True)
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
    await enqueue_access_action_for_state(
        session,
        project_id=project_id,
        customer_id=action.customer_id,
        resource_id=action.resource_id,
        available_at=moment,
    )
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

    created = 0
    cursor_change: datetime | None = None
    cursor_id: UUID | None = None
    batch_size = max(limit, 100)

    while created < limit:
        statement = select(EffectiveAccess).where(
            EffectiveAccess.project_id == project_id,
            EffectiveAccess.next_change_at.is_not(None),
            EffectiveAccess.next_change_at <= moment,
        )
        if cursor_change is not None and cursor_id is not None:
            statement = statement.where(
                or_(
                    EffectiveAccess.next_change_at > cursor_change,
                    and_(
                        EffectiveAccess.next_change_at == cursor_change,
                        EffectiveAccess.id > cursor_id,
                    ),
                )
            )

        due = tuple(
            (
                await session.scalars(
                    statement.order_by(
                        EffectiveAccess.next_change_at,
                        EffectiveAccess.id,
                    ).limit(batch_size)
                )
            ).all()
        )
        if not due:
            break

        for effective in due:
            boundary = effective.next_change_at
            if boundary is None:
                continue
            cursor_change = boundary
            cursor_id = effective.id
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
                if created >= limit:
                    break

    await session.flush()
    return created
