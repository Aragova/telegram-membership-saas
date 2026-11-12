"""Periodic source-linked Telegram Stars reward evidence.

Runs on the EXISTING shard-zero customer supervisor and NEVER initiates
merchant billing. All original file delivery and subscription access remain
independent of this accounting reconciler.
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
class StarEvidenceResult:
    examined: int
    recorded: int
    unresolved: int
    billing_activated: bool
    refunds_recorded: int = 0
    refunds_pending_source: int = 0
    subscriptions_recorded: int = 0
    subscriptions_unresolved: int = 0
    subscription_refunds_recorded: int = 0
    subscription_refunds_unresolved: int = 0


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
        subs = (await session.execute(text(
            "SELECT public.platform_saas_capture_new_subscription_stars("
            ":limit) AS data"
        ).columns(data=JSONB), {"limit": limit})).scalar_one()
        sub_refunds = (await session.execute(text(
            "SELECT public.platform_saas_capture_subscription_star_refunds("
            ":limit) AS data"
        ).columns(data=JSONB), {"limit": limit})).scalar_one()
        refunds = (await session.execute(text(
            "SELECT public.platform_saas_capture_star_refunds("
            ":limit) AS data"
        ).columns(data=JSONB), {"limit": limit})).scalar_one()
    if not all(isinstance(v, dict)
               for v in (data, refunds, subs, sub_refunds)):
        raise RuntimeError("Invalid Stars source/refund evidence response")
    result = StarEvidenceResult(
        examined=int(data["examined"]),
        recorded=int(data["recorded"]),
        unresolved=int(data["unresolved"]),
        billing_activated=bool(data["billing_activated"]),
        refunds_recorded=int(refunds["recorded"]),
        refunds_pending_source=int(refunds["waiting_for_original_sale"]),
        subscriptions_recorded=int(subs["recorded"]),
        subscriptions_unresolved=int(subs["unresolved"]),
        subscription_refunds_recorded=int(sub_refunds["recorded"]),
        subscription_refunds_unresolved=int(
            sub_refunds["waiting_for_original_sale"]
        ),
    )
    if result.unresolved:
        LOG.warning(
            "Unresolved Stars accounting sources: %s in current batch",
            result.unresolved,
        )
    if result.recorded:
        LOG.info("Recorded %s paid-file Stars reward snapshots", result.recorded)
    if result.refunds_recorded:
        LOG.info(
            "Recorded %s immutable Telegram Stars refund credits",
            result.refunds_recorded,
        )
    if result.refunds_pending_source:
        LOG.warning(
            "Stars refund sources awaiting sale attribution: %s",
            result.refunds_pending_source,
        )
    return result


async def merchant_star_project_summary(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, project_id: UUID, month: date,
) -> dict:
    """Tenant owner's read-only priced Stars sales (never an invoice).

    Results include the outstanding reconciliation count. Missing rows
    cannot be silently interpreted as 'no Stars sold'.
    """
    if month.day != 1:
        raise ValueError("Stars accounting month must start on day 1")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id, project_id=project_id),
    ) as session:
        data = (await session.execute(text(
            "SELECT public.platform_saas_star_project_summary("
            ":project,:month) AS data"
        ).columns(data=JSONB), {
            "project": project_id, "month": month,
        })).scalar_one()
        credits = (await session.execute(text(
            "SELECT public.platform_saas_star_project_refund_summary("
            ":project,:month) AS data"
        ).columns(data=JSONB), {
            "project": project_id, "month": month,
        })).scalar_one()
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("rows"), list)
        or not isinstance(credits, dict)
    ):
        raise RuntimeError("Invalid owner Stars settlement preview")
    return {**data, **credits}
