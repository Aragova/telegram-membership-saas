"""Tenant-isolated daily owner digests for paid subscription deadlines.

A project-local 10:00 scan enqueues at most one digest per day. The existing
outbox delivers it in the owner's language and rechecks the current paid
subscription state immediately before Telegram transmission.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.telegram_dates import native_telegram_time

REMINDER_DAYS = (7, 3, 1)
DIGEST_HOUR = 10
MAX_DISPLAY_ENTRIES = 14


@dataclass(frozen=True, slots=True)
class PaidDeadline:
    customer_id: UUID
    plan_id: UUID
    ends_at: datetime
    status: str
    started_at: datetime
    username: str | None
    first_name: str | None
    plan_name: str


@dataclass(frozen=True, slots=True)
class PaidCoverage:
    """Another confirmed paid subscription potentially covering old resources."""

    customer_id: UUID
    plan_id: UUID
    ends_at: datetime | None
    started_at: datetime


def exclude_replaced_deadlines(
    candidates: list[PaidDeadline], *,
    coverages: list[PaidCoverage],
    plan_resources: dict[UUID, set[UUID]],
    checked_at: datetime,
) -> list[PaidDeadline]:
    """Exclude an older plan only when all its channels remain paid elsewhere.

    A replacement can consist of several plans. Each covering term must
    remain effective past the old deadline; a partial replacement is not enough.
    Lifetime coverage is represented by ends_at=None.
    """
    by_customer: dict[UUID, list[PaidCoverage]] = {}
    for cover in coverages:
        if (
            cover.started_at <= checked_at
            and (cover.ends_at is None or cover.ends_at > checked_at)
        ):
            by_customer.setdefault(cover.customer_id, []).append(cover)

    visible = []
    for old in candidates:
        required = plan_resources.get(old.plan_id, set())
        alternatives = [
            cover for cover in by_customer.get(old.customer_id, [])
            if cover.plan_id != old.plan_id
            and (cover.ends_at is None or cover.ends_at > old.ends_at)
        ]
        paid_elsewhere = {
            resource for cover in alternatives
            for resource in plan_resources.get(cover.plan_id, set())
        }
        if required and required.issubset(paid_elsewhere):
            continue
        visible.append(old)
    return visible


def _due_time(day: date, timezone: str) -> datetime:
    return datetime.combine(day, time(DIGEST_HOUR), tzinfo=ZoneInfo(timezone))


def next_digest_check(now: datetime, timezone: str) -> datetime:
    """UTC instant of next 10:00 project-local slot, DST-aware."""
    if now.tzinfo is None:
        raise ValueError("Expiry scheduler requires an aware clock")
    local = now.astimezone(ZoneInfo(timezone))
    today_due = _due_time(local.date(), timezone)
    target = today_due if local < today_due else _due_time(
        local.date() + timedelta(days=1), timezone,
    )
    return target.astimezone(UTC)


def _display_person(row: PaidDeadline) -> str:
    if row.username:
        return "@" + escape(row.username[:64])
    if row.first_name:
        return escape(row.first_name[:55])
    return tr("Покупатель", "Customer") + " #" + row.customer_id.hex[:7].upper()


def classify_paid_deadlines(
    rows: list[PaidDeadline], *, local_date: date, timezone: str,
) -> dict[int, list[PaidDeadline]]:
    """One customer per category, no stale original term after renewal."""
    zone = ZoneInfo(timezone)
    cutoff = _due_time(local_date, timezone)
    previous = _due_time(local_date - timedelta(days=1), timezone)
    by_category: dict[int, dict[UUID, PaidDeadline]] = {
        7: {}, 3: {}, 1: {}, 0: {},
    }
    for row in rows:
        ends_local = row.ends_at.astimezone(zone)
        if row.started_at > cutoff.astimezone(UTC):
            continue
        days = (ends_local.date() - local_date).days
        if (
            row.status == "active" and row.ends_at > cutoff.astimezone(UTC)
            and days in REMINDER_DAYS
        ):
            by_category[days].setdefault(row.customer_id, row)
        elif (
            row.status in ("active", "expired")
            and previous.astimezone(UTC) < row.ends_at <= cutoff.astimezone(UTC)
        ):
            by_category[0].setdefault(row.customer_id, row)
    return {
        key: sorted(group.values(), key=lambda item: (
            item.ends_at, item.username or "", item.customer_id.hex,
        ))
        for key, group in by_category.items()
    }


async def _read_deadline_rows(
    session: AsyncSession, *, project_id: UUID, local_date: date, timezone: str,
) -> list[PaidDeadline]:
    """Read paid subscription end dates, not manual access grants or invoices."""
    lower = _due_time(local_date - timedelta(days=1), timezone).astimezone(UTC)
    upper = datetime.combine(
        local_date + timedelta(days=8), time.min,
        tzinfo=ZoneInfo(timezone),
    ).astimezone(UTC)
    stmt = (
        select(
            Subscription.customer_id,
            Subscription.plan_id,
            Subscription.current_period_end,
            Subscription.status,
            Subscription.started_at,
            TelegramUser.username,
            TelegramUser.first_name,
            Plan.name,
        )
        .join(
            ProjectCustomer,
            and_(
                ProjectCustomer.id == Subscription.customer_id,
                ProjectCustomer.project_id == Subscription.project_id,
            ),
        )
        .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
        .join(
            Plan,
            and_(Plan.id == Subscription.plan_id,
                 Plan.project_id == Subscription.project_id),
        )
        .where(
            Subscription.project_id == project_id,
            Subscription.status.in_(("active", "expired")),
            Subscription.current_period_end.is_not(None),
            Subscription.current_period_end > lower,
            Subscription.current_period_end < upper,
        )
        .order_by(Subscription.current_period_end, Subscription.customer_id)
    )
    return [
        PaidDeadline(*record)
        for record in (await session.execute(stmt)).all()
    ]


async def read_expiry_digest(
    session: AsyncSession, *, project_id: UUID, local_date: date,
    timezone: str, checked_at: datetime | None = None,
) -> dict[int, list[PaidDeadline]]:
    rows = await _read_deadline_rows(
        session, project_id=project_id, local_date=local_date, timezone=timezone,
    )
    if not rows:
        return {7: [], 3: [], 1: [], 0: []}

    # Do not tell an owner that a customer is expiring if a different PAID
    # plan (including lifetime) already covers every resource of the old one.
    # A different currency/plan ID is not sufficient evidence of replacement:
    # compare the actual channel resources, with tenant boundaries.
    checked = checked_at or _due_time(local_date, timezone).astimezone(UTC)
    customers = {row.customer_id for row in rows}
    covering = (
        await session.execute(
            select(
                Subscription.customer_id, Subscription.plan_id,
                Subscription.current_period_end, Subscription.started_at,
            ).where(
                Subscription.project_id == project_id,
                Subscription.customer_id.in_(customers),
                Subscription.status == "active",
                Subscription.started_at <= checked,
                (
                    Subscription.current_period_end.is_(None)
                    | (Subscription.current_period_end > checked)
                ),
            )
        )
    ).all()
    coverages = [PaidCoverage(*row) for row in covering]
    plan_ids = {row.plan_id for row in rows}
    plan_ids.update(item.plan_id for item in coverages)
    links = (
        await session.execute(
            select(PlanResource.plan_id, PlanResource.resource_id).where(
                PlanResource.project_id == project_id,
                PlanResource.plan_id.in_(plan_ids),
            )
        )
    ).all()
    plan_resources: dict[UUID, set[UUID]] = {}
    for plan_id, resource_id in links:
        plan_resources.setdefault(plan_id, set()).add(resource_id)
    rows = exclude_replaced_deadlines(
        rows, coverages=coverages,
        plan_resources=plan_resources, checked_at=checked,
    )
    return classify_paid_deadlines(
        rows, local_date=local_date, timezone=timezone,
    )


def render_expiry_digest(
    groups: dict[int, list[PaidDeadline]], *, local_date: date, timezone: str,
) -> str | None:
    if not any(groups.values()):
        return None
    lines = [
        tr("⏳ <b>Сроки подписок</b>", "⏳ <b>Subscription deadlines</b>"),
        f"🗓️ {local_date:%d.%m.%Y} · {escape(timezone)}",
    ]
    remaining = MAX_DISPLAY_ENTRIES
    for day in (*REMINDER_DAYS, 0):
        entries = groups.get(day, [])
        if not entries:
            continue
        if day == 0:
            label = tr("🔴 Оплаченный срок истёк", "🔴 Paid term ended")
        else:
            label = (
                tr(f"🔔 Через {day} дн.", f"🔔 In {day} days")
                if day != 1 else tr("🔔 Завтра", "🔔 Tomorrow")
            )
        lines += ["", f"<b>{label} · {len(entries)}</b>"]
        shown = entries[:remaining]
        remaining -= len(shown)
        for row in shown:
            deadline = native_telegram_time(row.ends_at, timezone, fmt="dt")
            lines.append(
                "• " + _display_person(row) + " · " + escape(row.plan_name[:48])
                + " · " + deadline
            )
        extra = len(entries) - len(shown)
        if extra:
            lines.append(tr(f"… и ещё {extra}", f"… and {extra} more"))
    lines += ["", tr(
        "Указаны оплаченные сроки, а не подтверждение удаления из канала.",
        "These are paid-term deadlines, not confirmed channel removals.",
    )]
    return "\n".join(lines)


def expiry_digest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=tr("👥 Подписчики", "👥 Subscribers"),
            callback_data="own:subscribers",
        ),
    ]])


async def schedule_owner_expiry_digest(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    now: datetime,
) -> datetime:
    """Enqueue today's 10:00 digest, unique by project/day/owner; no DB migration."""
    if now.tzinfo is None:
        raise ValueError("Expiry scheduler requires an aware clock")
    from telegram_membership_saas.modules.owner_notifications import (
        _owner_telegram_id,
        _queue,
        read_owner_settings,
    )

    async with system_project_session_scope(factory, project_id) as session:
        project = await session.get(Project, project_id)
        if project is None:
            return now.astimezone(UTC) + timedelta(days=1)
        zone = project.timezone
        local = now.astimezone(ZoneInfo(zone))
        due = _due_time(local.date(), zone)
        if local < due:
            return due.astimezone(UTC)
        next_slot = _due_time(local.date() + timedelta(days=1), zone).astimezone(UTC)
        if project.status != "active":
            return next_slot
        flags = await read_owner_settings(session, project_id)
        if not flags["expiry"]:
            return next_slot
        groups = await read_expiry_digest(
            session, project_id=project_id, local_date=local.date(), timezone=zone,
            checked_at=now,
        )
        if not any(groups.values()):
            return next_slot
        owner = await _owner_telegram_id(session, project_id)
        if owner is not None:
            await _queue(
                session, project_id=project_id, kind="expiry",
                event_key=local.date().isoformat(), recipient=owner,
                payload={"local_date": local.date().isoformat(), "timezone": zone},
            )
        return next_slot


async def prepare_expiry_delivery(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    telegram_id: int, payload: dict, now: datetime,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Recheck toggles, project owner, current paid terms and local date at send."""
    from telegram_membership_saas.modules.owner_notifications import (
        _owner_telegram_id,
        read_owner_settings,
    )

    async with system_project_session_scope(factory, project_id) as session:
        project = await session.get(Project, project_id)
        if project is None or project.status != "active":
            return None
        if payload.get("timezone") != project.timezone:
            return None
        local_now = now.astimezone(ZoneInfo(project.timezone))
        local_date = local_now.date()
        if payload.get("local_date") != local_date.isoformat():
            return None
        if local_now < _due_time(local_date, project.timezone):
            return None
        if not (await read_owner_settings(session, project_id))["expiry"]:
            return None
        if await _owner_telegram_id(session, project_id) != telegram_id:
            return None
        groups = await read_expiry_digest(
            session, project_id=project_id, local_date=local_date,
            timezone=project.timezone, checked_at=now,
        )
        body = render_expiry_digest(
            groups, local_date=local_date, timezone=project.timezone,
        )
        return (body, expiry_digest_keyboard()) if body else None
