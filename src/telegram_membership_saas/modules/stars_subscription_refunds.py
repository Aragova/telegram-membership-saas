"""Authenticated Telegram Stars subscription refund and access correction.

A real Telegram refunded_payment service update is the ONLY ingress. The
original paid charge remains historically paid and the refund has separate
immutable evidence. No buyer-submitted proof can alter an entitlement.
"""
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.access.service import reconcile_access_target
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant, EntitlementGrantStatus, EntitlementSource,
)
from telegram_membership_saas.modules.stars_subscriptions import (
    SubscriptionStarOrder, SubscriptionStarRefund,
)
from telegram_membership_saas.modules.subscriptions import (
    Subscription, SubscriptionStatus,
)


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
    refund = SubscriptionStarRefund(
        project_id=project_id, order_id=order.id,
        telegram_bot_id=bot_id, telegram_user_id=buyer_id,
        invoice_payload=payload, telegram_charge_id=charge_id,
        stars_amount=amount, refunded_at=moment,
    )
    session.add(refund)
    # Explicit DB trigger independently validates immutable paid order.
    await session.flush()
    grants = (await session.scalars(select(EntitlementGrant).where(
        EntitlementGrant.project_id == project_id,
        EntitlementGrant.source == EntitlementSource.STARS_PAYMENT.value,
        EntitlementGrant.source_id == order.id,
        EntitlementGrant.customer_id == order.customer_id,
        EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
    ).with_for_update())).all()
    targets = set()
    for grant in grants:
        grant.status = EntitlementGrantStatus.REVOKED.value
        grant.revoked_at = max(moment, grant.valid_from)
        targets.add(grant.resource_id)
    subscription = await session.scalar(select(Subscription).where(
        Subscription.project_id == project_id,
        Subscription.customer_id == order.customer_id,
        Subscription.plan_id == order.plan_id,
    ).with_for_update())
    # The last payment points at this order only if no newer manual/Stars
    # purchase superseded it; never revoke a different paid plan.
    if subscription is not None and subscription.last_star_order_id == order.id:
        subscription.status = SubscriptionStatus.CANCELLED.value
    await session.flush()
    for resource_id in sorted(targets):
        await reconcile_access_target(
            session, project_id=project_id,
            customer_id=order.customer_id, resource_id=resource_id,
        )
    return refund.id
