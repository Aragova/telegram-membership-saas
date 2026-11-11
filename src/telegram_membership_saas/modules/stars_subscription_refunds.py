"""Authenticated Telegram Stars subscription refund and access correction.

A real Telegram refunded_payment service update is the ONLY ingress. The
original paid charge remains historically paid and the refund has separate
immutable evidence. No buyer-submitted proof can alter an entitlement.
"""
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.access.service import reconcile_access_target
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import ProjectCustomer
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
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock


async def record_verified_subscription_star_refund(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int, payload: str, amount: int, currency: str,
    charge_id: str, refunded_at: datetime,
) -> UUID:
    if (
        currency != "XTR" or amount < 1 or amount > 250_000
        or not payload.startswith("sp:") or not charge_id
        or len(charge_id) > 256 or refunded_at.tzinfo is None
    ):
        raise ValueError("Invalid authenticated Stars refund")
    moment = refunded_at.astimezone(UTC)
    if moment > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("Future-dated Stars refund")
    # Payment approvals, manual owner termination and refunds must share
    # one serialization point; otherwise their row-lock order deadlocks.
    await checkout_retirement_lock(session, project_id)
    order = await session.scalar(select(SubscriptionStarOrder).where(
        SubscriptionStarOrder.project_id == project_id,
        SubscriptionStarOrder.telegram_bot_id == bot_id,
        SubscriptionStarOrder.telegram_user_id == buyer_id,
        SubscriptionStarOrder.invoice_payload == payload,
    ).with_for_update())
    if (
        order is None or order.status != "paid"
        or order.telegram_charge_id != charge_id
        or order.stars_amount != amount
        or order.paid_at is None or moment < order.paid_at
    ):
        raise PermissionError("Stars refund mismatches paid subscription")
    previous = await session.scalar(select(SubscriptionStarRefund).where(
        SubscriptionStarRefund.project_id == project_id,
        SubscriptionStarRefund.order_id == order.id,
    ))
    if previous is not None:
        if (
            previous.telegram_charge_id != charge_id
            or previous.stars_amount != amount
            or previous.telegram_bot_id != bot_id
            or previous.telegram_user_id != buyer_id
            or previous.invoice_payload != payload
        ):
            raise PermissionError("Conflicting Stars refund")
        return previous.id
    # Mirror successful-payment lock ordering: project -> order ->
    # customer -> subscription -> grant. Preserve earlier purchased periods.
    customer = await session.scalar(select(ProjectCustomer.id).where(
        ProjectCustomer.project_id == project_id,
        ProjectCustomer.id == order.customer_id,
    ).with_for_update())
    if customer is None:
        raise RuntimeError("Refund customer disappeared")
    subscription = await session.scalar(select(Subscription).where(
        Subscription.project_id == project_id,
        Subscription.customer_id == order.customer_id,
        Subscription.plan_id == order.plan_id,
    ).with_for_update())
    if subscription is None:
        raise RuntimeError("Refund subscription disappeared")
    refund = SubscriptionStarRefund(
        project_id=project_id, order_id=order.id,
        telegram_bot_id=bot_id, telegram_user_id=buyer_id,
        invoice_payload=payload, telegram_charge_id=charge_id,
        stars_amount=amount, refunded_at=moment,
    )
    session.add(refund)
    # The PostgreSQL trigger independently validates the original charge.
    await session.flush()
    grants = (await session.scalars(select(EntitlementGrant).where(
        EntitlementGrant.project_id == project_id,
        EntitlementGrant.source == EntitlementSource.STARS_PAYMENT.value,
        EntitlementGrant.source_id == order.id,
        EntitlementGrant.customer_id == order.customer_id,
        EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
    ).with_for_update())).all()
    targets: set[UUID] = set()
    for grant in grants:
        grant.status = EntitlementGrantStatus.REVOKED.value
        grant.revoked_at = max(moment, grant.valid_from)
        targets.add(grant.resource_id)
    await session.flush()
    # The subscription is a summary of multiple independently paid grants.
    # A returned renewal must not cancel an earlier still-paid period.
    manual = (await session.scalars(
        select(EntitlementGrant)
        .join(Payment, and_(
            Payment.id == EntitlementGrant.source_id,
            Payment.project_id == EntitlementGrant.project_id,
        ))
        .where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.customer_id == order.customer_id,
            EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
            EntitlementGrant.source == EntitlementSource.PAYMENT.value,
            Payment.customer_id == order.customer_id,
            Payment.plan_id == order.plan_id,
        )
    )).all()
    star_rows = (await session.execute(
        select(EntitlementGrant, SubscriptionStarOrder)
        .join(SubscriptionStarOrder, and_(
            SubscriptionStarOrder.id == EntitlementGrant.source_id,
            SubscriptionStarOrder.project_id == EntitlementGrant.project_id,
        ))
        .where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.customer_id == order.customer_id,
            EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
            EntitlementGrant.source == EntitlementSource.STARS_PAYMENT.value,
            SubscriptionStarOrder.customer_id == order.customer_id,
            SubscriptionStarOrder.plan_id == order.plan_id,
        )
        .with_for_update(of=EntitlementGrant)
    )).all()
    # A late Telegram refund may arrive AFTER a later payment was already
    # confirmed. B's grant may have been scheduled from refunded A's expiry,
    # despite A having actually been returned BEFORE B was purchased.
    # Rebuild the subsequent Stars periods from original paid_at snapshots,
    # without altering any independent/manual payment or the sale ledger.
    subsequent: dict[UUID, tuple[SubscriptionStarOrder, list[EntitlementGrant]]] = {}
    for grant, paid_order in star_rows:
        if paid_order.paid_at is not None and paid_order.paid_at > moment:
            if paid_order.id not in subsequent:
                subsequent[paid_order.id] = (paid_order, [])
            subsequent[paid_order.id][1].append(grant)
    if subsequent:
        project = await session.scalar(select(Project).where(
            Project.id == project_id,
        ))
        if project is None:
            raise RuntimeError("Refund project disappeared")
        # Keep paid periods created before the verified refund as anchors.
        # Later grants must not inherit the expiry of the returned purchase.
        anchor_ends = [
            grant.valid_until for grant in manual
            if grant.valid_until is not None and grant.valid_from <= moment
        ] + [
            grant.valid_until for grant, paid_order in star_rows
            if paid_order.paid_at is not None and paid_order.paid_at <= moment
            and grant.valid_until is not None
        ]
        prior_end = max(anchor_ends, default=None)
        for paid_order, paid_grants in sorted(
            subsequent.values(),
            key=lambda pair: (pair[0].paid_at, str(pair[0].id)),
        ):
            actual_start = (
                max(paid_order.paid_at, prior_end)
                if prior_end is not None else paid_order.paid_at
            )
            actual_end = calculate_period_end(
                actual_start, timezone_name=project.timezone,
                duration_value=paid_order.duration_value,
                duration_unit=paid_order.duration_unit,
            )
            for grant in paid_grants:
                if grant.valid_from != actual_start or grant.valid_until != actual_end:
                    grant.valid_from = actual_start
                    grant.valid_until = actual_end
                    targets.add(grant.resource_id)
            prior_end = actual_end
        await session.flush()
    stars = [grant for grant, _ in star_rows]
    survivors = (*manual, *stars)
    if survivors:
        # Lifetime entitlements outrank finite grants; otherwise preserve
        # the longest remaining purchased end date, then latest start.
        end_floor = datetime.min.replace(tzinfo=UTC)
        winning = max(
            survivors,
            key=lambda g: (
                g.valid_until is None,
                g.valid_until or end_floor,
                g.valid_from,
            ),
        )
        subscription.current_period_start = winning.valid_from
        subscription.current_period_end = winning.valid_until
        subscription.status = (
            SubscriptionStatus.ACTIVE.value
            if winning.valid_until is None or winning.valid_until > moment
            else SubscriptionStatus.EXPIRED.value
        )
        if winning.source == EntitlementSource.PAYMENT.value:
            subscription.last_payment_id = winning.source_id
            subscription.last_star_order_id = None
        else:
            subscription.last_payment_id = None
            subscription.last_star_order_id = winning.source_id
    else:
        subscription.status = SubscriptionStatus.CANCELLED.value
        # Preserve the historical last payer link for audit/FK invariants.
    await session.flush()
    for resource_id in sorted(targets):
        await reconcile_access_target(
            session, project_id=project_id,
            customer_id=order.customer_id, resource_id=resource_id,
        )
    return refund.id
