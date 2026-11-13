"""Production ViMi Pay monthly invoice issuer, using existing shard-zero bot supervisor.

No per-bot timer, external payment charge, or synthetic test account. Runs
only when the platform owner explicitly activates billing in central Finances.
All source proof, access scope and invoice uniqueness are checked by SQL.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.tenant_context import TenantContext


async def reconcile_commercial_invoices(
    factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> dict:
    if settings.platform_owner_telegram_id is None:
        return {"enabled": False, "reason": "no owner"}
    actor_id = await ensure_telegram_actor(
        factory, telegram_id=settings.platform_owner_telegram_id,
        username=None, first_name=None,
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        snapshot = (await session.execute(text(
            "SELECT public.platform_saas_issue_due_batch() AS data"
        ).columns(data=JSONB))).scalar_one()
    if not isinstance(snapshot, dict):
        raise RuntimeError("Invalid SaaS monthly invoice reconciliation")
    return snapshot
