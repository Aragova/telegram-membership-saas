"""Optional project-local 10:00 financial summary for a verified project owner.

Only confirmed manual Payments and native Stars orders count as sales. Pending invoices,
screenshots, and noncommercial access never become revenue. Monetary values
remain in their original currencies without a fabricated FX conversion.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.currency_display import display_currency
from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.merchant_revenue import merchant_confirmed_sales
from telegram_membership_saas.modules.owner_expiry import _due_time
from telegram_membership_saas.modules.owner_notifications import (
    _owner_telegram_id,
    _queue,
    read_owner_settings,
)
from telegram_membership_saas.modules.payments import PaymentRequest
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.subscriptions import Subscription

MAX_CURRENCIES = 24


@dataclass(frozen=True, slots=True)
class DailyFinancialSnapshot:
    project_name: str
    timezone: str
    report_date: date
    approved_count: int
    approved_totals: tuple[tuple[str, Decimal, int], ...]
    new_customers: int
    active_paid_customers: int
    waiting_review: int
    as_of: datetime


def previous_local_day_window(
    report_date: date, timezone: str,
) -> tuple[datetime, datetime]:
    """UTC-open/closed boundaries, respecting 23/25-hour DST days."""
    begin = datetime.combine(
        report_date, time.min, tzinfo=ZoneInfo(timezone),
    ).astimezone(UTC)
    end = datetime.combine(
        report_date + timedelta(days=1), time.min, tzinfo=ZoneInfo(timezone),
    ).astimezone(UTC)
    return begin, end


async def read_daily_financial_snapshot(
    session: AsyncSession, *, project: Project, report_date: date,
    now: datetime,
) -> DailyFinancialSnapshot:
    """Read-only, strictly single-project SQL aggregates at delivery time."""
    begin, end = previous_local_day_window(report_date, project.timezone)
    sales = merchant_confirmed_sales(project.id)
    payments = (
        await session.execute(
            select(
                sales.c.currency_code,
                func.sum(sales.c.amount),
                func.count(),
            )
            .where(
                sales.c.paid_at >= begin,
                sales.c.paid_at < end,
            )
            .group_by(sales.c.currency_code)
            .order_by(sales.c.currency_code)
        )
    ).all()
    totals = tuple(
        (currency, Decimal(amount), int(count))
        for currency, amount, count in payments
    )
    newcomers = int(await session.scalar(
        select(func.count(ProjectCustomer.id)).where(
            ProjectCustomer.project_id == project.id,
            ProjectCustomer.created_at >= begin,
            ProjectCustomer.created_at < end,
        )
    ) or 0)
    active_paid = int(await session.scalar(
        select(func.count(distinct(Subscription.customer_id))).where(
            Subscription.project_id == project.id,
            Subscription.status == "active",
            Subscription.started_at <= now,
            or_(
                Subscription.current_period_end.is_(None),
                Subscription.current_period_end > now,
            ),
        )
    ) or 0)
    review = int(await session.scalar(
        select(func.count(PaymentRequest.id)).where(
            PaymentRequest.project_id == project.id,
            PaymentRequest.status == "waiting_review",
        )
    ) or 0)
    return DailyFinancialSnapshot(
        project_name=project.name,
        timezone=project.timezone,
        report_date=report_date,
        approved_count=sum(count for _, _, count in totals),
        approved_totals=totals,
        new_customers=newcomers,
        active_paid_customers=active_paid,
        waiting_review=review,
        as_of=now,
    )


def _amount(value: Decimal) -> str:
    label = format(value, "f")
    return label.rstrip("0").rstrip(".") if "." in label else label


def render_daily_financial_summary(snapshot: DailyFinancialSnapshot) -> str:
    """Bilingual safe HTML; a compact aggregate, never a bank statement."""
    lines = [
        tr("📊 <b>Ежедневный отчёт</b>", "📊 <b>Daily financial summary</b>"),
        "📁 " + escape(snapshot.project_name[:100]),
        (
            tr("🗓️ Подтверждённые оплаты за ", "🗓️ Approved payments for ")
            + f"{snapshot.report_date:%d.%m.%Y}"
        ),
        "🕙 " + escape(snapshot.timezone),
        "",
        (
            tr("✅ Оплат подтверждено: ", "✅ Approved payments: ")
            + str(snapshot.approved_count)
        ),
    ]
    for currency, amount, count in snapshot.approved_totals[:MAX_CURRENCIES]:
        lines.append(
            "💰 <b>" + escape(_amount(amount)) + " "
            + escape(display_currency(currency)) + "</b> · "
            + tr("операций: ", "payments: ") + str(count)
        )
    overflow = len(snapshot.approved_totals) - MAX_CURRENCIES
    if overflow > 0:
        lines.append(tr(
            f"… ещё валют: {overflow}. Подробности в статистике.",
            f"… {overflow} more currencies. See Statistics.",
        ))
    if not snapshot.approved_totals:
        lines.append(tr("💰 Подтверждённых оплат нет", "💰 No approved payments"))
    lines.extend([
        "",
        (
            tr("🆕 Новых пользователей: ", "🆕 New customers: ")
            + str(snapshot.new_customers)
        ),
        (
            tr("👥 Активных платных подписчиков: ",
               "👥 Active paying subscribers: ")
            + str(snapshot.active_paid_customers)
        ),
        (
            tr("📩 Заявок на проверке: ", "📩 Awaiting review: ")
            + str(snapshot.waiting_review)
        ),
        "🕒 " + tr("Состояние на ", "Status as of ")
        + f"{snapshot.as_of.astimezone(ZoneInfo(snapshot.timezone)):%d.%m.%Y %H:%M}",
        "",
        tr(
            "Суммы — ручные оплаты и подтверждённые Telegram Stars. "
            "Проверяйте поступления в банке отдельно.",
            "Amounts include approved manual payments and Telegram Stars. "
            "Verify bank deposits separately.",
        ),
    ])
    return "\n".join(lines)


def daily_financial_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=tr("📊 Статистика", "📊 Statistics"),
            callback_data="own:statistics:7",
        ),
    ]])


async def schedule_owner_daily_financial(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, now: datetime,
) -> datetime:
    """At most one queued event per project-local date. Disabled by default."""
    if now.tzinfo is None:
        raise ValueError("Daily summary scheduler requires an aware clock")
    async with system_project_session_scope(factory, project_id) as session:
        project = await session.get(Project, project_id)
        if project is None:
            return now.astimezone(UTC) + timedelta(days=1)
        local = now.astimezone(ZoneInfo(project.timezone))
        due = _due_time(local.date(), project.timezone)
        if local < due:
            return due.astimezone(UTC)
        next_slot = _due_time(
            local.date() + timedelta(days=1), project.timezone,
        ).astimezone(UTC)
        if project.status != "active":
            return next_slot
        flags = await read_owner_settings(session, project_id)
        if not flags["daily"]:
            return next_slot
        recipient = await _owner_telegram_id(session, project_id)
        if recipient is not None:
            await _queue(
                session, project_id=project_id, kind="daily",
                event_key=local.date().isoformat(), recipient=recipient,
                payload={
                    "local_date": local.date().isoformat(),
                    "timezone": project.timezone,
                },
            )
        return next_slot


async def prepare_owner_daily_financial(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, recipient: int, payload: dict, now: datetime,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Revalidate owner, opt-in, timezone and day immediately before send."""
    if now.tzinfo is None:
        raise ValueError("Daily summary requires an aware clock")
    async with system_project_session_scope(factory, project_id) as session:
        project = await session.get(Project, project_id)
        if project is None or project.status != "active":
            return None
        if payload.get("timezone") != project.timezone:
            return None
        local = now.astimezone(ZoneInfo(project.timezone))
        if payload.get("local_date") != local.date().isoformat():
            return None
        if local < _due_time(local.date(), project.timezone):
            return None
        if not (await read_owner_settings(session, project_id))["daily"]:
            return None
        if (await _owner_telegram_id(session, project_id)) != recipient:
            return None
        previous_date = local.date() - timedelta(days=1)
        snapshot = await read_daily_financial_snapshot(
            session, project=project, report_date=previous_date, now=now,
        )
        return (
            render_daily_financial_summary(snapshot),
            daily_financial_keyboard(),
        )
