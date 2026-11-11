"""Tenant-safe channel cards, paid-only deletion guard and sales statistics.

No confirmed financial facts or payment requests are ever deleted here.
A removed channel is hidden from setup/sales; its chat remains operational
for any existing invoices, manual grants and access reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.merchant_revenue import merchant_confirmed_sales
from telegram_membership_saas.modules.entitlements import EntitlementGrant
from telegram_membership_saas.modules.payments import Payment, PaymentRequest
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.stars_subscriptions import SubscriptionStarOrder
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class ChannelInsights:
    resource_id: UUID
    name: str
    external_id: str
    resource_type: str
    active_plans: int
    paid_orders: int
    paid_customers: int
    active_paid_customers: int
    pending_invoices: int
    sales_by_currency: tuple[tuple[str, Decimal, int], ...]


def _linked_plans(project_id: UUID, resource_id: UUID):
    return select(PlanResource.plan_id).where(
        PlanResource.project_id == project_id,
        PlanResource.resource_id == resource_id,
    )


async def read_channel_insights(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    project_id: UUID, resource_id: UUID,
    now: datetime | None = None,
) -> ChannelInsights:
    """Read confirmed payments, not drafts/manual access as sales."""
    moment = now or datetime.now(UTC)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        channel = await session.scalar(select(Resource).where(
            Resource.id == resource_id, Resource.project_id == project_id,
            Resource.removed_at.is_(None),
        ))
        if channel is None:
            raise ValueError("Канал больше не доступен в этом боте")
        ids = _linked_plans(project_id, resource_id)
        plans = int(await session.scalar(select(func.count(Plan.id)).where(
            Plan.project_id == project_id, Plan.id.in_(ids),
            Plan.status == "active", Plan.removed_at.is_(None),
        )) or 0)
        sales = merchant_confirmed_sales(project_id)
        paid_query = select(
            sales.c.currency_code,
            func.sum(sales.c.amount),
            func.count(),
        ).where(
            sales.c.plan_id.in_(_linked_plans(project_id, resource_id)),
        ).group_by(sales.c.currency_code).order_by(sales.c.currency_code)
        totals = tuple(
            (currency, Decimal(amount), int(count))
            for currency, amount, count in (await session.execute(paid_query)).all()
        )
        customers = int(await session.scalar(select(
            func.count(distinct(sales.c.customer_id)),
        ).where(
            sales.c.plan_id.in_(_linked_plans(project_id, resource_id)),
        )) or 0)
        active = int(await session.scalar(select(
            func.count(distinct(Subscription.customer_id)),
        ).where(
            Subscription.project_id == project_id,
            Subscription.plan_id.in_(_linked_plans(project_id, resource_id)),
            Subscription.status == "active",
            Subscription.started_at <= moment,
            or_(Subscription.current_period_end.is_(None),
                Subscription.current_period_end > moment),
        )) or 0)
        pending = int(await session.scalar(select(
            func.count(PaymentRequest.id),
        ).where(
            PaymentRequest.project_id == project_id,
            PaymentRequest.plan_id.in_(_linked_plans(project_id, resource_id)),
            PaymentRequest.status.in_(("waiting_confirmation", "waiting_review")),
        )) or 0)
        return ChannelInsights(
            resource_id=channel.id, name=channel.name,
            external_id=channel.external_id, resource_type=channel.resource_type,
            active_plans=plans, paid_orders=sum(count for _, _, count in totals),
            paid_customers=customers, active_paid_customers=active,
            pending_invoices=pending, sales_by_currency=totals,
        )


async def remove_channel_without_paid_purchases(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, resource_id: UUID,
) -> tuple[str, int]:
    """Hide channel and associated plans, retaining unpaid invoices and access.

    The channel stays ACTIVE internally so a submitted, but unreviewed,
    payment may still be settled and delivered. Its prices/plan references
    are immutable, while plans are removed from new checkout. An existing
    confirmed purchase (even an expired one) forbids this operation.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        channel = await session.scalar(select(Resource).where(
            Resource.id == resource_id, Resource.project_id == project_id,
        ).with_for_update())
        if channel is None or channel.removed_at is not None:
            raise ValueError("Канал уже удалён или недоступен")
        paid = await session.scalar(select(Payment.id).where(
            Payment.project_id == project_id,
            Payment.plan_id.in_(_linked_plans(project_id, resource_id)),
        ).limit(1))
        paid_grant = await session.scalar(select(EntitlementGrant.id).where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.resource_id == resource_id,
            EntitlementGrant.source.in_(("payment", "stars_payment")),
        ).limit(1))
        paid_stars = await session.scalar(select(SubscriptionStarOrder.id).where(
            SubscriptionStarOrder.project_id == project_id,
            SubscriptionStarOrder.plan_id.in_(_linked_plans(project_id, resource_id)),
            SubscriptionStarOrder.status == "paid",
        ).limit(1))
        if paid is not None or paid_grant is not None or paid_stars is not None:
            raise ValueError(
                "В канале есть подтверждённые покупки. Удаление запрещено: "
                "нужно сохранить историю платежей и доступ покупателей."
            )
        moment = datetime.now(UTC)
        channel.removed_at = moment
        plans = (await session.scalars(select(Plan).where(
            Plan.project_id == project_id,
            Plan.id.in_(_linked_plans(project_id, resource_id)),
        ).with_for_update())).all()
        for plan in plans:
            plan.status = "archived"
            plan.removed_at = moment
        await session.flush()
        return channel.name, len(plans)
