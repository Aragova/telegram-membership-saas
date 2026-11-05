"""Read-only project statistics for connected owner bots.

All business reads run in a tenant-scoped PostgreSQL transaction. Manual and
service access are never counted as customer payments or paid subscribers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.entitlements import EntitlementGrant
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.payments import Payment, PaymentRequest
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.tenant_context import TenantContext

PERIOD_DAYS = {"7": 7, "30": 30, "all": None}


@dataclass(frozen=True, slots=True)
class OwnerStatistics:
    project_name: str
    timezone: str
    period: str
    since: date | None
    through: date
    payment_count: int
    currency_totals: tuple[tuple[str, Decimal, int], ...]
    total_customers: int
    new_customers: int
    active_paid_customers: int
    expiring_paid_customers: int
    active_manual_grants: int
    waiting_review: int


def _period_start(
    period: str, *, now: datetime, timezone: str,
) -> tuple[datetime | None, date | None, date]:
    if period not in PERIOD_DAYS:
        raise ValueError("Неизвестный период статистики")
    local_today = now.astimezone(ZoneInfo(timezone)).date()
    count = PERIOD_DAYS[period]
    if count is None:
        return None, None, local_today
    local_start = local_today - timedelta(days=count - 1)
    local_midnight = datetime.combine(local_start, time.min, tzinfo=ZoneInfo(timezone))
    return local_midnight.astimezone(UTC), local_start, local_today


async def read_owner_statistics(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, period: str = "7",
    now: datetime | None = None,
) -> OwnerStatistics:
    """Aggregates actual confirmed sales by their original currencies only."""
    if period not in PERIOD_DAYS:
        raise ValueError("Неизвестный период статистики")
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("Statistics clock must be timezone-aware")
    moment = moment.astimezone(UTC)

    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        project: Project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        cutoff, since, through = _period_start(
            period, now=moment, timezone=project.timezone,
        )

        payment_query = select(
            Payment.currency_code, func.sum(Payment.final_amount),
            func.count(Payment.id),
        ).where(Payment.project_id == project_id)
        if cutoff is not None:
            payment_query = payment_query.where(Payment.approved_at >= cutoff)
        payment_query = payment_query.group_by(Payment.currency_code).order_by(
            Payment.currency_code,
        )
        totals = tuple(
            (currency, Decimal(amount), int(count))
            for currency, amount, count in (await session.execute(payment_query)).all()
        )

        customer_query = select(func.count(ProjectCustomer.id)).where(
            ProjectCustomer.project_id == project_id,
        )
        total_customers = int(await session.scalar(customer_query) or 0)
        if cutoff is not None:
            customer_query = customer_query.where(ProjectCustomer.created_at >= cutoff)
        new_customers = int(await session.scalar(customer_query) or 0)

        paid_active = (
            Subscription.project_id == project_id,
            Subscription.status == "active",
            # Early renewal moves current_period_start into the future while
            # the original paid term remains effective from started_at.
            Subscription.started_at <= moment,
            or_(
                Subscription.current_period_end.is_(None),
                Subscription.current_period_end > moment,
            ),
        )
        active_paid = int(await session.scalar(
            select(func.count(distinct(Subscription.customer_id))).where(*paid_active)
        ) or 0)
        expiring_paid = int(await session.scalar(
            select(func.count(distinct(Subscription.customer_id))).where(
                *paid_active,
                Subscription.current_period_end.is_not(None),
                Subscription.current_period_end <= moment + timedelta(days=7),
            )
        ) or 0)
        manual_grants = int(await session.scalar(
            select(func.count(EntitlementGrant.id)).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source == "manual",
                EntitlementGrant.status == "active",
                EntitlementGrant.valid_from <= moment,
                or_(
                    EntitlementGrant.valid_until.is_(None),
                    EntitlementGrant.valid_until > moment,
                ),
            )
        ) or 0)
        pending = int(await session.scalar(
            select(func.count(PaymentRequest.id)).where(
                PaymentRequest.project_id == project_id,
                PaymentRequest.status == "waiting_review",
            )
        ) or 0)
    return OwnerStatistics(
        project_name=project.name, timezone=project.timezone,
        period=period, since=since, through=through,
        payment_count=sum(c for _, _, c in totals), currency_totals=totals,
        total_customers=total_customers, new_customers=new_customers,
        active_paid_customers=active_paid,
        expiring_paid_customers=expiring_paid,
        active_manual_grants=manual_grants, waiting_review=pending,
    )
