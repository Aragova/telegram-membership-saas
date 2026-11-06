"""Read-only, audited and authorization-gated platform aggregate reports.

The only cross-project SQL is a fixed SECURITY DEFINER PostgreSQL function
which checks a grant maintained by the migration database owner. The regular
tenant and payment services never inherit platform permissions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.tenant_context import TenantContext

PERIODS = ("day", "7", "30", "365", "all")
PAGE_SIZE = 6

PERIOD_LABELS = {
    "day": "Сегодня",
    "7": "7 дней",
    "30": "30 дней",
    "365": "Год",
    "all": "Всё время",
}


@dataclass(frozen=True, slots=True)
class PlatformSnapshot:
    period: str
    page: int
    counts: dict
    bots: tuple[dict, ...]
    turnover: tuple[dict, ...]
    pages: int


def is_configured_platform_owner(
    telegram_user_id: int, settings: Settings | None = None,
) -> bool:
    current = settings or get_settings()
    return (
        current.platform_owner_telegram_id is not None
        and current.platform_owner_telegram_id == telegram_user_id
    )


def period_start(
    period: str, *, settings: Settings | None = None,
    now: datetime | None = None,
) -> datetime:
    from datetime import timedelta

    if period not in PERIODS:
        raise ValueError("Неизвестный период")
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("Datetime must include timezone")
    tz = ZoneInfo((settings or get_settings()).platform_timezone)
    local = moment.astimezone(tz)
    if period == "all":
        return datetime(1970, 1, 1, tzinfo=UTC)
    if period == "day":
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    return (local - timedelta(days=int(period))).astimezone(UTC)


async def platform_report(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, actor_telegram_id: int,
    period: str = "30", page: int = 0,
    settings: Settings | None = None,
) -> PlatformSnapshot:
    """Trust the Telegram identity, then the database's operator ACL."""
    current = settings or get_settings()
    if not is_configured_platform_owner(actor_telegram_id, current):
        raise PermissionError("Нет доступа к управлению платформой")
    if not 0 <= page <= 100000:
        raise ValueError("Недопустимая страница")
    since = period_start(period, settings=current)
    query = text(
        "SELECT public.platform_report_snapshot(:since, :page, :size) AS snapshot"
    ).columns(snapshot=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        report = (await session.execute(
            query, {"since": since, "page": page, "size": PAGE_SIZE},
        )).scalar_one()
    if not isinstance(report, dict):
        raise RuntimeError("Platform analytics returned an invalid snapshot")
    return PlatformSnapshot(
        period=period, page=page, counts=report,
        bots=tuple(report.get("bot_rows", [])),
        turnover=tuple(report.get("gmv_by_currency", [])),
        pages=int(report.get("bot_pages", 0)),
    )


def fmt_turnover(value: str) -> str:
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError("Invalid turnover")
    # Decimal.normalize() rounds to the current context precision (28 by
    # default), corrupting the display of valid numeric(38,18) totals.
    # Fixed-point formatting retains every source digit without rounding.
    rendered = format(amount, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
