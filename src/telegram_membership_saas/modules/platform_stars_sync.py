"""Periodic source-linked Telegram Stars reward evidence.

Runs on the EXISTING shard-zero customer supervisor and NEVER initiates
merchant billing. All original file delivery and subscription access remain
independent of this accounting reconciler.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.tenant_context import TenantContext

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StarEvidenceResult:
    examined: int
    recorded: int
    unresolved: int
    billing_activated: bool


async def capture_paid_file_stars(
    factory: async_sessionmaker[AsyncSession], settings: Settings,
    *, limit: int = 200,
) -> StarEvidenceResult:
    """Capture versioned official reward quotes for paid files.

    Only the configured database-provisioned platform owner can call the
    stored function. All direct financial ledger table privileges are denied
    to the runtime role; no per-bot Telegram API calls or checkout changes.
    """
    if settings.platform_owner_telegram_id is None:
        return StarEvidenceResult(0, 0, 0, False)
    if not 1 <= limit <= 200:
        raise ValueError("Invalid Stars batch size")
    actor = await ensure_telegram_actor(
        factory, telegram_id=settings.platform_owner_telegram_id,
        username=None, first_name=None,
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor),
    ) as session:
        data = (await session.execute(text(
            "SELECT public.platform_saas_capture_new_file_stars("
            ":limit) AS data"
        ).columns(data=JSONB), {"limit": limit})).scalar_one()
    if not isinstance(data, dict):
        raise RuntimeError("Invalid Stars source evidence response")
    result = StarEvidenceResult(
        examined=int(data["examined"]),
        recorded=int(data["recorded"]),
        unresolved=int(data["unresolved"]),
        billing_activated=bool(data["billing_activated"]),
    )
    if result.unresolved:
        LOG.warning(
            "Unresolved Stars accounting sources: %s in current batch",
            result.unresolved,
        )
    if result.recorded:
        LOG.info("Recorded %s paid-file Stars reward snapshots", result.recorded)
    return result
