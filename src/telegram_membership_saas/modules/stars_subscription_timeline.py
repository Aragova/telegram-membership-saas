"""Chronological Stars subscription periods shared by payment and refund paths.

All callers hold the project checkout/retirement lock. Manual grants are
immutable: use *approval time*, not grant.valid_from, to determine whether a
prepaid manual period preceded a Stars purchase.
"""
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant, EntitlementGrantStatus, EntitlementSource,
)
from telegram_membership_saas.modules.payments import Payment
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.stars_subscriptions import SubscriptionStarOrder
from telegram_membership_saas.modules.subscriptions import (
    Subscription, SubscriptionStatus, calculate_period_end,
)


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
        )
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


def manual_anchor_end(
    manual: list[tuple[EntitlementGrant, Payment]], at: datetime,
) -> datetime | None:
    """A future-dated manual grant still anchors a later Stars charge."""
    return max((
        grant.valid_until for grant, payment in manual
        if payment.approved_at <= at and grant.valid_until is not None
    ), default=None)


def historical_stars_start(
    at: datetime,
    manual: list[tuple[EntitlementGrant, Payment]],
    stars: list[tuple[EntitlementGrant, SubscriptionStarOrder]],
) -> datetime:
    """Never extend an earlier recovered charge from a later purchase."""
    anchors = [
        grant.valid_until for grant, order in stars
        if order.paid_at is not None and order.paid_at <= at
        and grant.valid_until is not None
    ]
    manual_end = manual_anchor_end(manual, at)
    if manual_end is not None:
        anchors.append(manual_end)
    return max([at, *anchors])


async def rebuild_stars_windows_after(
    session: AsyncSession, *, project_id: UUID, customer_id: UUID,
    plan_id: UUID, after: datetime,
) -> set[UUID]:
    """Replay still-paid later Stars renewals after a late charge/refund.

    Manual periods are not rewritten. An earlier refund never shifts a
    later Stars purchase ahead of a prepayment already manually approved.
    """
    manual, stars = await load_surviving_paid_sources(
        session, project_id=project_id, customer_id=customer_id,
        plan_id=plan_id,
    )
    future: dict[UUID, tuple[SubscriptionStarOrder, list[EntitlementGrant]]] = {}
    prior_stars = [
        grant.valid_until for grant, order in stars
        if order.paid_at is not None and order.paid_at <= after
        and grant.valid_until is not None
    ]
    for grant, order in stars:
        if order.paid_at is not None and order.paid_at > after:
            if order.id not in future:
                future[order.id] = (order, [])
            future[order.id][1].append(grant)
    if not future:
        return set()
    project = await session.scalar(select(Project).where(
        Project.id == project_id,
    ))
    if project is None:
        raise RuntimeError("Paid subscription project disappeared")
    changed: set[UUID] = set()
    for order, grants in sorted(
        future.values(), key=lambda pair: (pair[0].paid_at, str(pair[0].id)),
    ):
        prior_end = max((
            *prior_stars,
            *([manual_anchor_end(manual, order.paid_at)]
              if manual_anchor_end(manual, order.paid_at) is not None else []),
        ), default=None)
        start_at = max(order.paid_at, prior_end) if prior_end is not None else order.paid_at
        end_at = calculate_period_end(
            start_at, timezone_name=project.timezone,
            duration_value=order.duration_value, duration_unit=order.duration_unit,
        )
        for grant in grants:
            if grant.valid_from != start_at or grant.valid_until != end_at:
                grant.valid_from = start_at
                grant.valid_until = end_at
                changed.add(grant.resource_id)
        if end_at is not None:
            prior_stars.append(end_at)
    if changed:
        await session.flush()
    return changed


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
        # Historical payment references are retained for audit.
        await session.flush()
        return
    floor = datetime.min.replace(tzinfo=UTC)
    winning = max(survivors, key=lambda grant: (
        grant.valid_until is None,
        grant.valid_until or floor,
        grant.valid_from,
    ))
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
