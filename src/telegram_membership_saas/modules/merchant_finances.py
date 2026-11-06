"""Read-only merchant finances for the configured ViMi Pay platform owner.

The report uses the original immutable payment-event amounts and currencies.
No customer, bank, invoice or Telegram credentials are included.
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


@dataclass(frozen=True, slots=True)
class MerchantFinances:
    period: str
    page: int
    pages: int
    merchants_total: int
    unmapped_payments: int
    businesses: tuple[dict, ...]


async def platform_merchant_finances(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    period: str = "30",
    page: int = 0,
    settings: Settings | None = None,
) -> MerchantFinances:
    current = settings or get_settings()
    if not is_configured_platform_owner(actor_telegram_id, current):
        raise PermissionError("Нет доступа к финансовой статистике платформы")
    if not 0 <= page <= 100000:
        raise ValueError("Недопустимая страница")
    since = period_start(period, settings=current)
    query = text(
        "SELECT public.platform_merchant_finances(:since, :page, :size) AS snapshot"
    ).columns(snapshot=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        data = (await session.execute(
            query, {"since": since, "page": page, "size": PAGE_SIZE},
        )).scalar_one()
    if not isinstance(data, dict) or not isinstance(data.get("merchants"), list):
        raise RuntimeError("Merchant finance report returned invalid data")
    return MerchantFinances(
        period=period,
        page=page,
        pages=int(data["pages"]),
        merchants_total=int(data["merchant_count"]),
        unmapped_payments=int(data["unmapped_payments"]),
        businesses=tuple(data["merchants"]),
    )
