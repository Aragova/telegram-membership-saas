"""Reconcile paid manual and Telegram Stars subscription periods in event time.

Payment history and original charge/refund evidence are immutable. Only
still-active entitlement grant windows are repaired under the shared
project checkout lock. Owner termination events are read through a
strictly project-scoped database function, never through owner-only RLS.
"""
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.payments import Payment
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.stars_subscriptions import (
    SubscriptionStarOrder,
    SubscriptionStarRefund,
)
from telegram_membership_saas.modules.subscriptions import (
    Subscription,
    SubscriptionStatus,
    calculate_period_end,
)


async def owner_end_times(
    session: AsyncSession, *, project_id: UUID,
    subscription: Subscription | None,
) -> tuple[datetime, ...]:
    """Scoped DEF-INER read is needed because system receipt has no owner ID."""
    if subscription is None:
        return ()
    timestamps = await session.scalar(select(
        func.public.subscription_owner_end_times(project_id, subscription.id)
    ))
    return tuple(timestamps or ())


def _terminated_between(
    termination_times: tuple[datetime, ...],
    original_paid: datetime, later_paid: datetime,
) -> bool:
    return any(original_paid <= ended <= later_paid for ended in termination_times)


async def load_surviving_paid_sources(
    session: AsyncSession, *, project_id: UUID, customer_id: UUID,
    plan_id: UUID,
) -> tuple[list[tuple[EntitlementGrant, Payment]],
           list[tuple[EntitlementGrant, SubscriptionStarOrder]]]:
    manual = (await session.execute(
        select(EntitlementGrant, Payment).join(Payment, and_(
            Payment.id == EntitlementGrant.source_id,
            Payment.project_id == EntitlementGrant.project_id,
        )).where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.customer_id == customer_id,
            EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
            EntitlementGrant.source == EntitlementSource.PAYMENT.value,
            Payment.customer_id == customer_id,
            Payment.plan_id == plan_id,
        ).with_for_update(of=EntitlementGrant)
    )).all()
    stars = (await session.execute(
        select(EntitlementGrant, SubscriptionStarOrder)
        .join(SubscriptionStarOrder, and_(
            SubscriptionStarOrder.id == EntitlementGrant.source_id,
            SubscriptionStarOrder.project_id == EntitlementGrant.project_id,
        )).where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.customer_id == customer_id,
            EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
            EntitlementGrant.source == EntitlementSource.STARS_PAYMENT.value,
            SubscriptionStarOrder.customer_id == customer_id,
            SubscriptionStarOrder.plan_id == plan_id,
            SubscriptionStarOrder.status == "paid",
        ).with_for_update(of=EntitlementGrant)
    )).all()
    return list(manual), list(stars)


async def load_refunded_star_sources(
    session: AsyncSession, *, project_id: UUID, customer_id: UUID,
    plan_id: UUID,
) -> list[tuple[EntitlementGrant, SubscriptionStarOrder, SubscriptionStarRefund]]:
    rows = (await session.execute(
        select(EntitlementGrant, SubscriptionStarOrder, SubscriptionStarRefund)
        .join(SubscriptionStarOrder, and_(
            SubscriptionStarOrder.id == EntitlementGrant.source_id,
            SubscriptionStarOrder.project_id == EntitlementGrant.project_id,
        ))
        .join(SubscriptionStarRefund, and_(
            SubscriptionStarRefund.order_id == SubscriptionStarOrder.id,
            SubscriptionStarRefund.project_id == SubscriptionStarOrder.project_id,
        ))
        .where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.customer_id == customer_id,
            EntitlementGrant.source == EntitlementSource.STARS_PAYMENT.value,
            EntitlementGrant.status == EntitlementGrantStatus.REVOKED.value,
            SubscriptionStarOrder.customer_id == customer_id,
            SubscriptionStarOrder.plan_id == plan_id,
        )
    )).all()
    return list(rows)


def manual_anchor_end(
    manual: list[tuple[EntitlementGrant, Payment]],
    at: datetime,
    termination_times: tuple[datetime, ...] = (),
) -> datetime | None:
    """Prepaid manual grants anchor Stars by approval, not future valid_from."""
    return max((
        grant.valid_until for grant, payment in manual
        if payment.approved_at <= at and grant.valid_until is not None
        and not _terminated_between(termination_times, payment.approved_at, at)
    ), default=None)


def historical_stars_start(
    at: datetime,
    manual: list[tuple[EntitlementGrant, Payment]],
    stars: list[tuple[EntitlementGrant, SubscriptionStarOrder]],
    refunded: list[tuple[
        EntitlementGrant, SubscriptionStarOrder, SubscriptionStarRefund
    ]] = (),
    termination_times: tuple[datetime, ...] = (),
    timezone_name: str = "UTC",
) -> datetime:
    """Place an old charge using fully replayed historical refunded anchors.

    Refunded grants are immutable audit rows. Their *virtual* expiry may
    need recalculating from an older, only recently recovered paid charge
    BEFORE the newly confirmed B is placed. Owner termination cuts anchors,
    and a refund at the same second precedes a different new purchase.
    """
    anchors = [
        grant.valid_until for grant, order in stars
        if order.paid_at is not None and order.paid_at <= at
        and grant.valid_until is not None
        and not _terminated_between(termination_times, order.paid_at, at)
    ]
    manual_end = manual_anchor_end(manual, at, termination_times)
    if manual_end is not None:
        anchors.append(manual_end)

    prior_returned: list[tuple[datetime, datetime, datetime]] = []
    unique: dict[UUID, tuple[SubscriptionStarOrder, SubscriptionStarRefund]] = {}
    for _, order, returned in refunded:
        if order.paid_at is not None and order.paid_at <= at:
            unique[order.id] = (order, returned)
    for order, returned in sorted(
        unique.values(), key=lambda pair: (pair[0].paid_at, str(pair[0].id)),
    ):
        paid_at = order.paid_at
        if paid_at is None:
            continue
        source_ends = [
            grant.valid_until for grant, payment in manual
            if payment.approved_at <= paid_at and grant.valid_until is not None
            and not _terminated_between(
                termination_times, payment.approved_at, paid_at,
            )
        ]
        source_ends.extend(
            grant.valid_until for grant, paid_order in stars
            if paid_order.paid_at is not None
            and paid_order.paid_at <= paid_at and grant.valid_until is not None
            and not _terminated_between(
                termination_times, paid_order.paid_at, paid_at,
            )
        )
        source_ends.extend(
            valid_end for prior_paid, valid_end, returned_at in prior_returned
            if prior_paid <= paid_at and returned_at > paid_at
            and not _terminated_between(termination_times, prior_paid, paid_at)
        )
        virtual_start = max([paid_at, *source_ends])
        virtual_end = calculate_period_end(
            virtual_start, timezone_name=timezone_name,
            duration_value=order.duration_value, duration_unit=order.duration_unit,
        )
        if virtual_end is not None:
            prior_returned.append((paid_at, virtual_end, returned.refunded_at))
    anchors.extend(
        valid_end for original_paid, valid_end, returned_at in prior_returned
        if original_paid <= at and returned_at > at
        and not _terminated_between(termination_times, original_paid, at)
    )
    return max([at, *anchors])


async def rebuild_paid_windows_after(
    session: AsyncSession, *, project_id: UUID, customer_id: UUID,
    plan_id: UUID, after: datetime,
    termination_times: tuple[datetime, ...] = (),
    include_equal: bool = False,
) -> set[UUID]:
    """Replay later still-paid manual AND Stars renewals after backfill/refund.

    Recomputes active grants and virtual returned periods in chronological
    purchase order; virtual returned periods anchor only purchases made
    before their real refund and without intervening owner termination.
    """
    manual, stars = await load_surviving_paid_sources(
        session, project_id=project_id, customer_id=customer_id,
        plan_id=plan_id,
    )
    # A revoked/refunded order may still have been a valid prepayment
    # anchor at the time a later charge was made. Replay its *virtual*
    # period too, rather than using a stale grant end after an old charge
    # is backfilled. No revoked grant or financial source is mutated.
    events: dict[tuple[str, UUID], tuple[
        datetime, int | None, str, list[EntitlementGrant], datetime | None
    ]] = {}
    anchors: list[tuple[datetime, datetime, datetime | None]] = []
    for grant, payment in manual:
        at = payment.approved_at
        if at < after or (at == after and not include_equal):
            if grant.valid_until is not None:
                anchors.append((at, grant.valid_until, None))
        else:
            key = ("manual", payment.id)
            if key not in events:
                events[key] = (
                    at, payment.duration_value, payment.duration_unit, [], None,
                )
            events[key][3].append(grant)
    for grant, order in stars:
        if order.paid_at is None:
            continue
        at = order.paid_at
        if at < after or (at == after and not include_equal):
            if grant.valid_until is not None:
                anchors.append((at, grant.valid_until, None))
        else:
            key = ("stars", order.id)
            if key not in events:
                events[key] = (
                    at, order.duration_value, order.duration_unit, [], None,
                )
            events[key][3].append(grant)
    if not events:
        return set()
    refunded = await load_refunded_star_sources(
        session, project_id=project_id, customer_id=customer_id,
        plan_id=plan_id,
    )
    for grant, order, returned in refunded:
        if order.paid_at is None:
            continue
        at = order.paid_at
        if at < after or (at == after and not include_equal):
            if grant.valid_until is not None:
                anchors.append((at, grant.valid_until, returned.refunded_at))
        else:
            key = ("refunded", order.id)
            if key not in events:
                events[key] = (
                    at, order.duration_value, order.duration_unit, [],
                    returned.refunded_at,
                )
    project = await session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise RuntimeError("Paid subscription project disappeared")
    targets: set[UUID] = set()
    for _, (paid_at, value, unit, grants, refunded_at) in sorted(
        events.items(), key=lambda item: (item[1][0], item[0][0], str(item[0][1])),
    ):
        ends = [
            period_end for original_at, period_end, returned_at in anchors
            if original_at <= paid_at
            and (returned_at is None or returned_at > paid_at)
            and not _terminated_between(termination_times, original_at, paid_at)
        ]
        start_at = max([paid_at, *ends])
        end_at = calculate_period_end(
            start_at, timezone_name=project.timezone,
            duration_value=value, duration_unit=unit,
        )
        for grant in grants:
            if grant.valid_from != start_at or grant.valid_until != end_at:
                grant.valid_from = start_at
                grant.valid_until = end_at
                targets.add(grant.resource_id)
        if end_at is not None:
            anchors.append((paid_at, end_at, refunded_at))
    if targets:
        await session.flush()
    return targets


async def refresh_paid_subscription_summary(
    session: AsyncSession, *, project_id: UUID, customer_id: UUID,
    plan_id: UUID, subscription: Subscription, as_of: datetime,
) -> None:
    """Projection only; grants remain the access authority and audit source."""
    manual, stars = await load_surviving_paid_sources(
        session, project_id=project_id, customer_id=customer_id,
        plan_id=plan_id,
    )
    survivors = [grant for grant, _ in manual] + [grant for grant, _ in stars]
    if not survivors:
        subscription.status = SubscriptionStatus.CANCELLED.value
        await session.flush()
        return
    floor = datetime.min.replace(tzinfo=UTC)
    winning = max(survivors, key=lambda grant: (
        grant.valid_until is None,
        grant.valid_until or floor,
        grant.valid_from,
    ))
    subscription.started_at = min(subscription.started_at, winning.valid_from)
    subscription.current_period_start = winning.valid_from
    subscription.current_period_end = winning.valid_until
    subscription.status = (
        SubscriptionStatus.ACTIVE.value
        if winning.valid_until is None or winning.valid_until > as_of
        else SubscriptionStatus.EXPIRED.value
    )
    if winning.source == EntitlementSource.PAYMENT.value:
        subscription.last_payment_id = winning.source_id
        subscription.last_star_order_id = None
    else:
        subscription.last_payment_id = None
        subscription.last_star_order_id = winning.source_id
    await session.flush()
