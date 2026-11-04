"""Immutable platform usage meter with strict operator authorization.

This is historical usage accounting, not an invoice engine. For each business
action, PostgreSQL triggers append events in the same transaction as the source
record. Authorized reports are fixed read-only cross-project aggregates.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.platform_dashboard import (
    PAGE_SIZE,
    is_configured_platform_owner,
    period_start,
)
from telegram_membership_saas.tenant_context import TenantContext

EVENT_LABELS = {
    "bot_connected": "Подключений ботов",
    "bot_removed": "Удалений ботов",
    "bot_disabled": "Отключений ботов",
    "bot_reenabled": "Повторных включений",
    "bot_deleting": "Запросов удаления",
    "plan_published": "Публикаций тарифов",
    "payment_approved": "Подтверждённых оплат",
    "customer_joined": "Новых покупателей",
    "subscription_started": "Новых подписок",
    "subscription_renewed": "Продлений подписок",
}
BOT_EVENT_LABELS = {
    "bot_connected": "подключён",
    "bot_removed": "удалён",
    "bot_disabled": "отключён",
    "bot_reenabled": "снова включён",
    "bot_deleting": "удаление запрошено",
}


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    period: str
    page: int
    pages: int
    event_total: int
    counts: dict[str, int]
    turnover: tuple[dict, ...]
    history: tuple[dict, ...]


async def platform_usage_report(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    period: str = "30",
    page: int = 0,
    settings: Settings | None = None,
) -> UsageSnapshot:
    current = settings or get_settings()
    if not is_configured_platform_owner(actor_telegram_id, current):
        raise PermissionError("Нет доступа к статистике платформы")
    if not 0 <= page <= 100000:
        raise ValueError("Недопустимая страница")
    since = period_start(period, settings=current)
    query = text(
        "SELECT public.platform_usage_snapshot(:since, :page, :size) AS snapshot"
    ).columns(snapshot=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        data = (await session.execute(
            query, {"since": since, "page": page, "size": PAGE_SIZE},
        )).scalar_one()
    if not isinstance(data, dict):
        raise RuntimeError("Platform usage report returned an invalid snapshot")
    raw_counts = data.get("event_counts", {})
    if not isinstance(raw_counts, dict):
        raise RuntimeError("Invalid usage event counters")
    return UsageSnapshot(
        period=period,
        page=page,
        pages=int(data.get("bot_event_pages", 0)),
        event_total=int(data.get("events_total", 0)),
        counts={name: int(raw_counts.get(name, 0)) for name in EVENT_LABELS},
        turnover=tuple(data.get("payments_by_currency", [])),
        history=tuple(data.get("bot_events", [])),
    )
