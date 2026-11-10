"""Authenticated ViMi Pay payment instructions and historical-owner invoices.

All cross-project financial reads go through narrow PostgreSQL SECURITY DEFINER
functions; tenant clients never receive direct grants on platform ledger tables.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.platform_saas_billing_service import (
    _check_actor,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class MerchantSaasStatements:
    total: int
    unpaid: int
    outstanding_rub: Decimal
    configured: bool
    instructions: str | None
    invoices: tuple[dict, ...]
    page: int


async def platform_saas_payment_details(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    settings: Settings | None = None,
) -> dict:
    _check_actor(actor_telegram_id, settings)
    stmt = text(
        "SELECT public.platform_saas_payment_details_read() AS data"
    ).columns(data=JSONB)
    preflight_stmt = text(
        "SELECT public.platform_saas_exemption_preflight() AS data"
    ).columns(data=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        result = (await session.execute(stmt)).scalar_one()
        preflight = (await session.execute(preflight_stmt)).scalar_one()
    if not isinstance(result, dict):
        raise RuntimeError("Invalid platform payment instructions")
    if not isinstance(preflight, dict):
        raise RuntimeError("Invalid platform exemption preflight")
    # Visible only to the platform owner; no invoice creation or policy write.
    return {**result, "preflight": preflight}


async def configure_platform_saas_payment(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    instructions: str | None,
    settings: Settings | None = None,
) -> None:
    _check_actor(actor_telegram_id, settings)
    if instructions is not None:
        instructions = instructions.strip()
        if len(instructions) < 8 or len(instructions) > 900:
            raise ValueError("Bank instructions must contain 8 to 900 characters")
        # HTML is escaped at presentation; Telegram formatting is not trusted.
        if "\x00" in instructions:
            raise ValueError("Invalid bank instructions")
    stmt = text(
        "SELECT public.platform_saas_payment_details_set(:instructions)"
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        updated = (await session.execute(
            stmt, {"instructions": instructions},
        )).scalar_one()
        if not updated:
            raise RuntimeError("Platform payment configuration unavailable")


async def merchant_saas_statements(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    project_id: UUID | None = None,
    page: int = 0,
) -> MerchantSaasStatements:
    """Real *current* project owner reads their own historical account invoices.

    Moderators, transferred-in owners, other Merchants or Telegram customers
    cannot browse another liable owner's invoices.
    """
    if not 0 <= page <= 100000:
        raise ValueError("Invalid merchant billing page")
    stmt = text(
        "SELECT public.platform_saas_owner_invoice_report(:page,6) AS data"
    ).columns(data=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id, project_id=project_id),
    ) as session:
        if project_id is not None:
            holder = await session.scalar(
                select(Project.owner_user_id).where(Project.id == project_id)
            )
            if holder != actor_user_id:
                raise PermissionError(
                    "Only actual project owner may view SaaS statements"
                )
        # Central /billing works even after a user transferred out their
        # final project. Liability follows historical account, not the bot.
        data = (await session.execute(stmt, {"page": page})).scalar_one()
    if not isinstance(data, dict) or not isinstance(data.get("invoices"), list):
        raise RuntimeError("Invalid merchant billing snapshot")
    return MerchantSaasStatements(
        total=int(data["total"]),
        unpaid=int(data["unpaid"]),
        outstanding_rub=Decimal(str(data["outstanding_rub"])),
        configured=bool(data["details_configured"]),
        instructions=data["instructions"],
        invoices=tuple(data["invoices"]),
        page=page,
    )
