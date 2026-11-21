"""Owner-scoped read-only fleet dashboard for central ViMi Pay.

One PostgreSQL round trip: independent of the number of connected bots.
The RPC checks actor ownership in the DB; no raw table grants or bot tokens.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.tenant_context import TenantContext


async def read_owner_network_home(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
) -> dict:
    stmt = text(
        "SELECT public.platform_saas_owner_network_home() AS snapshot"
    ).columns(snapshot=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        report = (await session.execute(stmt)).scalar_one()
    if not isinstance(report, dict):
        raise RuntimeError("Invalid ViMi Pay owner fleet summary")
    fleet = report.get("network")
    billing = report.get("billing")
    sales = report.get("revenue")
    if (
        not isinstance(fleet, dict)
        or not isinstance(billing, dict)
        or not isinstance(sales, list)
    ):
        raise RuntimeError("Incomplete ViMi Pay owner fleet summary")
    return report
