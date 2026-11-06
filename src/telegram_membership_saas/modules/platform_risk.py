"""Platform-owner-only, audited review of channel identity observations.

Observations are not fraud verdicts and do not make automated changes to
merchant identity, channel access, tenant projects, or billing.
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
class MerchantRiskSnapshot:
    period: str
    page: int
    pages: int
    total: int
    observations: tuple[dict, ...]


async def platform_merchant_risk_report(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    period: str = "30",
    page: int = 0,
    settings: Settings | None = None,
) -> MerchantRiskSnapshot:
    config = settings or get_settings()
    if not is_configured_platform_owner(actor_telegram_id, config):
        raise PermissionError("Нет доступа к журналу проверок платформы")
    if not 0 <= page <= 100000:
        raise ValueError("Недопустимая страница")
    since = period_start(period, settings=config)
    sql = text(
        "SELECT public.platform_merchant_risk_snapshot("
        ":since, :page, :size) AS snapshot"
    ).columns(snapshot=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        data = (await session.execute(
            sql, {"since": since, "page": page, "size": PAGE_SIZE},
        )).scalar_one()
    if not isinstance(data, dict) or not isinstance(data.get("observations"), list):
        raise RuntimeError("Platform risk review returned invalid data")
    return MerchantRiskSnapshot(
        period=period,
        page=page,
        pages=int(data["pages"]),
        total=int(data["total"]),
        observations=tuple(data["observations"]),
    )
