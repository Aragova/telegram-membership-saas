"""Verified native Stars subscription checkout: one charge, one durable grant.

No Telegram network calls occur inside the database transaction. The original
manual Payment -> Subscription -> Entitlement system remains unchanged; both
checkout types share the same Subscription, EntitlementGrant and access outbox.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.access.service import reconcile_access_target
from telegram_membership_saas.modules.bots.models import BotInstance
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    OutboxEventStatus,
    OutboxEventType,
)
from telegram_membership_saas.modules.payments.request_service import (
    get_or_create_project_customer,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.stars_subscription_timeline import (
    historical_stars_start,
    load_refunded_star_sources,
    load_surviving_paid_sources,
    owner_end_times,
    rebuild_paid_windows_after,
    refresh_paid_subscription_summary,
)
from telegram_membership_saas.modules.stars_subscriptions import (
    SubscriptionStarOrder,
)
from telegram_membership_saas.modules.subscriptions import (
    Subscription,
    SubscriptionStatus,
    calculate_period_end,
)
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock

STARS_MAX = 250_000
PAYMENT_WINDOW = timedelta(minutes=30)


async def create_stars_subscription_order(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int, username: str | None, first_name: str | None,
    plan_price_id: UUID,
) -> SubscriptionStarOrder:
    """Snapshot an active XTR plan/price and live resource before invoicing."""
    await checkout_retirement_lock(session, project_id)
    project = await session.scalar(select(Project).where(
        Project.id == project_id, Project.status == "active",
    ))
    bot = await session.scalar(select(BotInstance.id).where(
        BotInstance.project_id == project_id,
        BotInstance.telegram_bot_id == bot_id,
        BotInstance.status == "registered",
    ))
    if project is None or bot is None:
        raise ValueError("This connected bot is not ready for new orders")
    result = (await session.execute(
        select(Plan, PlanPrice).join(
            PlanPrice, (PlanPrice.project_id == Plan.project_id)
            & (PlanPrice.plan_id == Plan.id),
        ).where(
            Plan.project_id == project_id,
            Plan.status == "active", Plan.removed_at.is_(None),
            PlanPrice.id == plan_price_id, PlanPrice.currency_code == "XTR",
            PlanPrice.status == "active",
        ).with_for_update(of=Plan),
    )).first()
    if result is None:
        raise ValueError("Stars plan price is no longer available")
    plan, price = result
    if (
        price.amount != price.amount.to_integral_value()
        or not 1 <= price.amount <= STARS_MAX
    ):
        raise ValueError("Telegram Stars price must be a valid whole number")
    linked = await session.scalar(select(Resource.id).join(
        PlanResource, (PlanResource.project_id == Resource.project_id)
        & (PlanResource.resource_id == Resource.id),
    ).where(
        PlanResource.project_id == project_id,
        PlanResource.plan_id == plan.id,
        Resource.status == "active",
        Resource.removed_at.is_(None),
    ).limit(1))
    if linked is None:
        raise ValueError("Plan has no active channel or group")
    # Import at call time: telegram_commerce.__init__ loads handlers, which
    # import Stars recovery. A module-level import here creates a cycle
    # before the customer supervisor can start any of its Telegram workers.
    from telegram_membership_saas.modules.telegram_commerce.service import (
        upsert_telegram_user,
    )

    user = await upsert_telegram_user(
        session, telegram_user_id=buyer_id, username=username,
        first_name=first_name,
    )
    customer = await get_or_create_project_customer(
        session, project_id=project_id, user_id=user.id,
    )
    customer = await session.scalar(select(ProjectCustomer).where(
        ProjectCustomer.id == customer.id,
        ProjectCustomer.project_id == project_id,
    ).with_for_update())
    if customer is None or customer.is_banned:
        raise PermissionError("Buying is disabled for this account")
    order_id = uuid4()
    order = SubscriptionStarOrder(
        id=order_id,
        project_id=project_id,
        customer_id=customer.id,
        plan_id=plan.id,
        plan_price_id=price.id,
        telegram_bot_id=bot_id,
        telegram_user_id=buyer_id,
        invoice_payload="sp:" + order_id.hex,
        stars_amount=int(price.amount),
        plan_name_snapshot=plan.name,
        duration_value=plan.duration_value,
        duration_unit=plan.duration_unit,
        status="pending",
    )
    session.add(order)
    await session.flush()
    return order


async def stars_subscription_precheckout(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int, payload: str, currency: str, amount: int,
) -> bool:
    """Fail closed before Telegram charges, without changing any history."""
    if currency != "XTR" or not payload.startswith("sp:"):
        return False
    order = await session.scalar(select(SubscriptionStarOrder).where(
        SubscriptionStarOrder.project_id == project_id,
        SubscriptionStarOrder.telegram_bot_id == bot_id,
        SubscriptionStarOrder.telegram_user_id == buyer_id,
        SubscriptionStarOrder.invoice_payload == payload,
    ))
    if (order is None or order.status != "pending"
        or order.stars_amount != amount
        or order.created_at is None
        or datetime.now(UTC) - order.created_at >= PAYMENT_WINDOW):
        return False
    project = await session.scalar(select(Project).where(
        Project.id == project_id, Project.status == "active",
    ))
    plan = await session.scalar(select(Plan).where(
        Plan.id == order.plan_id, Plan.project_id == project_id,
        Plan.status == "active", Plan.removed_at.is_(None),
    ))
    price = await session.scalar(select(PlanPrice).where(
        PlanPrice.id == order.plan_price_id,
        PlanPrice.project_id == project_id,
        PlanPrice.status == "active",
        PlanPrice.currency_code == "XTR",
    ))
    customer = await session.scalar(select(ProjectCustomer).where(
        ProjectCustomer.id == order.customer_id,
        ProjectCustomer.project_id == project_id,
    ))
    linked = await session.scalar(
        select(PlanResource.resource_id).join(
            Resource, (Resource.id == PlanResource.resource_id)
            & (Resource.project_id == PlanResource.project_id),
        ).where(
            PlanResource.project_id == project_id,
            PlanResource.plan_id == order.plan_id,
            Resource.status == "active",
            Resource.removed_at.is_(None),
        ).limit(1)
    )
    bot_exists = await session.scalar(select(BotInstance.id).where(
        BotInstance.project_id == project_id,
        BotInstance.telegram_bot_id == bot_id,
        BotInstance.status == "registered",
    ))
    allowed = bool(
        project and plan and price and bot_exists and linked and customer
        and not customer.is_banned
    )
    return allowed


async def mark_stars_precheckout_approved(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int, payload: str, amount: int,
) -> bool:
    """Serialized with bot retirement, only after LIVE Telegram rights check."""
    await checkout_retirement_lock(session, project_id)
    if not await stars_subscription_precheckout(
        session, project_id=project_id, bot_id=bot_id,
        buyer_id=buyer_id, payload=payload, currency="XTR", amount=amount,
    ):
        return False
    order = await session.scalar(select(SubscriptionStarOrder).where(
        SubscriptionStarOrder.project_id == project_id,
        SubscriptionStarOrder.invoice_payload == payload,
        SubscriptionStarOrder.telegram_user_id == buyer_id,
        SubscriptionStarOrder.telegram_bot_id == bot_id,
        SubscriptionStarOrder.status == "pending",
    ).with_for_update())
    if order is None:
        return False
    order.precheckout_approved_at = datetime.now(UTC)
    await session.flush()
    return True


async def confirm_subscription_stars_payment(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int, payload: str, currency: str,
    amount: int, charge_id: str, paid_at: datetime,
) -> tuple[SubscriptionStarOrder, Subscription]:
    """Atomic, idempotent paid order + subscription + access events.

    Repeated successful_payment or ledger-recovery updates for the same charge
    return existing state without extending the membership twice.
    """
    if (currency != "XTR" or amount <= 0
        or not payload.startswith("sp:") or not charge_id
        or len(charge_id) > 256
        or paid_at.tzinfo is None):
        raise ValueError("Invalid verified Telegram Stars receipt")
    moment = paid_at.astimezone(UTC)
    if moment > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("Future Stars receipt cannot be accepted")
    await checkout_retirement_lock(session, project_id)
    order = await session.scalar(select(SubscriptionStarOrder).where(
        SubscriptionStarOrder.project_id == project_id,
        SubscriptionStarOrder.invoice_payload == payload,
        SubscriptionStarOrder.telegram_user_id == buyer_id,
        SubscriptionStarOrder.telegram_bot_id == bot_id,
    ).with_for_update())
    if order is None or order.stars_amount != amount:
        raise PermissionError("Stars receipt does not match a pending order")
    # Manual settlement locks customer before subscription; use the same
    # lock order to prevent conflicting renewals and cross-rail deadlocks.
    customer = await session.scalar(select(ProjectCustomer).where(
        ProjectCustomer.id == order.customer_id,
        ProjectCustomer.project_id == project_id,
    ).with_for_update())
    if customer is None:
        raise RuntimeError("Paid project customer disappeared")
    # Original price and active-plan state are snapshots: if a seller archives
    # a plan AFTER Telegram charged, we must still fulfil the paid order.
    subscription = await session.scalar(select(Subscription).where(
        Subscription.project_id == project_id,
        Subscription.customer_id == order.customer_id,
        Subscription.plan_id == order.plan_id,
    ).with_for_update())
    if order.status == "paid":
        if order.telegram_charge_id != charge_id or subscription is None:
            raise PermissionError("Conflicting or unfulfilled Stars charge")
        return order, subscription
    if order.status != "pending":
        raise PermissionError("Stars order is no longer payable")
    project = await session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise RuntimeError("Paid project disappeared")
    terminations = await owner_end_times(
        session, project_id=project_id, subscription=subscription,
    )
    # Authenticated original charges remain accounting evidence even when
    # an owner's later explicit termination revoked their access. The
    # owner-only RLS audit is read solely via a project-scoped DB function.
    if any(moment <= ended for ended in terminations):
        order.status = "paid"
        order.telegram_charge_id = charge_id
        order.paid_at = moment
        await session.flush()
        return order, subscription
    # A verified ledger receipt can arrive after a newer paid renewal.
    # Determine the interval from payment chronology, not the latest
    # subscription summary, which may belong to that newer purchase.
    earlier_manual, earlier_stars = await load_surviving_paid_sources(
        session, project_id=project_id, customer_id=order.customer_id,
        plan_id=order.plan_id,
    )
    earlier_refunds = await load_refunded_star_sources(
        session, project_id=project_id, customer_id=order.customer_id,
        plan_id=order.plan_id,
    )
    start_at = historical_stars_start(
        moment, earlier_manual, earlier_stars, earlier_refunds,
        terminations, project.timezone, current_order=order,
    )
    end_at = calculate_period_end(
        start_at, timezone_name=project.timezone,
        duration_value=order.duration_value,
        duration_unit=order.duration_unit,
    )
    order.status = "paid"
    order.telegram_charge_id = charge_id
    order.paid_at = moment
    await session.flush()
    if subscription is None:
        subscription = Subscription(
            project_id=project_id, customer_id=order.customer_id,
            plan_id=order.plan_id, status=SubscriptionStatus.ACTIVE.value,
            started_at=start_at, current_period_start=start_at,
            current_period_end=end_at, last_payment_id=None,
            last_star_order_id=order.id,
        )
        session.add(subscription)
    else:
        subscription.status = SubscriptionStatus.ACTIVE.value
        # A missed historical charge may predate the first locally processed
        # payment. Preserve the DB invariant started_at <= period_start.
        subscription.started_at = min(subscription.started_at, start_at)
        subscription.current_period_start = start_at
        subscription.current_period_end = end_at
        subscription.last_payment_id = None
        subscription.last_star_order_id = order.id

    resource_ids = tuple((await session.scalars(
        select(PlanResource.resource_id).where(
            PlanResource.project_id == project_id,
            PlanResource.plan_id == order.plan_id,
        ).order_by(PlanResource.resource_id),
    )).all())
    if not resource_ids:
        raise RuntimeError("Paid subscription has no resources")
    grants = [
        EntitlementGrant(
            project_id=project_id,
            customer_id=order.customer_id,
            resource_id=resource_id,
            source=EntitlementSource.STARS_PAYMENT.value,
            source_id=order.id,
            status=EntitlementGrantStatus.ACTIVE.value,
            valid_from=start_at,
            valid_until=end_at,
        )
        for resource_id in resource_ids
    ]
    session.add_all(grants)
    await session.flush()
    # If the receipt was recovered out of order, repair already-confirmed
    # later Stars grants. Manual grants retain their approved periods.
    repaired = await rebuild_paid_windows_after(
        session, project_id=project_id, customer_id=order.customer_id,
        plan_id=order.plan_id, after=moment,
        termination_times=terminations,
        include_equal=True,
    )
    await refresh_paid_subscription_summary(
        session, project_id=project_id, customer_id=order.customer_id,
        plan_id=order.plan_id, subscription=subscription,
        as_of=datetime.now(UTC),
    )
    for resource_id in sorted(repaired):
        await reconcile_access_target(
            session, project_id=project_id,
            customer_id=order.customer_id, resource_id=resource_id,
        )
    session.add_all([
        OutboxEvent(
            project_id=project_id,
            event_type=OutboxEventType.ACCESS_RECALCULATE.value,
            aggregate_type="entitlement_grant",
            aggregate_id=grant.id,
            dedupe_key=f"entitlement_grant:{grant.id}:access_recalculate",
            payload={
                "customer_id": str(order.customer_id),
                "resource_id": str(grant.resource_id),
                "grant_id": str(grant.id),
            },
            status=OutboxEventStatus.PENDING.value,
        )
        for grant in grants
    ])
    await session.flush()
    return order, subscription


async def live_stars_subscription_permissions(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    invoice_payload: str, bot: Bot,
) -> None:
    """Fail closed at precheckout if Telegram bot cannot issue live access.

    This is deliberately independent from customer_runtime to avoid a
    dispatcher/runtime circular import. Check exactly the resources attached
    to the order's priced plan, not just an unrelated channel.
    """
    async with system_project_session_scope(factory, project_id) as session:
        order = await session.scalar(select(SubscriptionStarOrder).where(
            SubscriptionStarOrder.project_id == project_id,
            SubscriptionStarOrder.telegram_bot_id == bot.id,
            SubscriptionStarOrder.invoice_payload == invoice_payload,
            SubscriptionStarOrder.status == "pending",
        ))
        if order is None:
            raise PermissionError("Stars order is not available")
        external_ids = (await session.scalars(
            select(Resource.external_id)
            .join(PlanResource,
                  (PlanResource.resource_id == Resource.id)
                  & (PlanResource.project_id == Resource.project_id))
            .where(
                PlanResource.project_id == project_id,
                PlanResource.plan_id == order.plan_id,
                Resource.status == "active",
                Resource.removed_at.is_(None),
            )
        )).all()
    if not external_ids:
        raise PermissionError("No deliverable channel for paid Stars plan")
    for chat_id in external_ids:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
            status = getattr(member.status, "value", member.status)
            if status != "creator" and (
                status != "administrator"
                or not member.can_invite_users
                or not member.can_restrict_members
            ):
                raise ValueError("Insufficient Telegram permissions")
        except Exception:
            raise RuntimeError(
                "Connected bot cannot issue or revoke live channel access"
            ) from None
