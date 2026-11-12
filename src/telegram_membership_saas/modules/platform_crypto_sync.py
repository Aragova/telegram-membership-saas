"""Read-only, owner-authorized Crypto Pay commission evidence.

Capture only genuine locally settled Crypto Pay invoices. The original
payment-time paid_usd_rate (crypto) or already published CBR sale-day fiat
rates are immutable; no monthly FX guesses, no bot invoice, no SaaS holds.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.tenant_context import TenantContext

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CryptoEvidenceResult:
    examined: int
    recorded: int
    unresolved: int
    billing_activated: bool


async def capture_crypto_sale_evidence(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings, *, limit: int = 200,
) -> CryptoEvidenceResult:
    """One bounded, shard-zero owner-only accounting capture, not billing."""
    if settings.platform_owner_telegram_id is None:
        return CryptoEvidenceResult(0, 0, 0, False)
    if not 1 <= limit <= 200:
        raise ValueError("Invalid Crypto Pay capture limit")
    owner = await ensure_telegram_actor(
        factory, telegram_id=settings.platform_owner_telegram_id,
        username=None, first_name=None,
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner),
    ) as session:
        row = (await session.execute(text(
            "SELECT public.platform_saas_capture_crypto_sales("
            ":limit) AS data"
        ).columns(data=JSONB), {"limit": limit})).scalar_one()
    if not isinstance(row, dict):
        raise RuntimeError("Invalid Crypto Pay capture response")
    result = CryptoEvidenceResult(
        examined=int(row["examined"]),
        recorded=int(row["recorded"]),
        unresolved=int(row["unresolved"]),
        billing_activated=bool(row["billing_activated"]),
    )
    if result.unresolved:
        LOG.warning(
            "Crypto Pay accounting evidence pending verified source or FX: %s",
            result.unresolved,
        )
    if result.recorded:
        LOG.info("Captured %s immutable Crypto Pay sale/USD snapshots", result.recorded)
    return result


async def crypto_sale_project_summary(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    project_id: UUID,
    billing_month: date,
) -> dict:
    """Historical owner-scoped read model for Crypto Pay GMV and 5% fee.

    Both charge creation and application remain OFF; pending/unknown valuation
    is separately reported, never quietly priced at zero.
    """
    if billing_month.day != 1:
        raise ValueError("Crypto Pay month must start on day one")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id, project_id=project_id),
    ) as session:
        data = (await session.execute(text(
            "SELECT public.platform_saas_crypto_project_summary("
            ":project,:month) AS data"
        ).columns(data=JSONB), {
            "project": project_id, "month": billing_month,
        })).scalar_one()
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise RuntimeError("Invalid Crypto Pay monthly sales preview")
    return data
