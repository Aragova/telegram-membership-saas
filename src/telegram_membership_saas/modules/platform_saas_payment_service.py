"""Authenticated ViMi Pay payment instructions and historical-owner invoices.

All cross-project financial reads go through narrow PostgreSQL SECURITY DEFINER
functions; tenant clients never receive direct grants on platform ledger tables.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
    pending_credit_rub: Decimal = Decimal("0")
    pending_credit_count: int = 0
    foreign_unpaid_count: int = 0
    due_now_count: int = 0
    due_now_rub: Decimal = Decimal("0")
    upcoming_count: int = 0
    upcoming_rub: Decimal = Decimal("0")


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
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        result = (await session.execute(stmt)).scalar_one()
    if not isinstance(result, dict):
        raise RuntimeError("Invalid platform payment instructions")
    return result


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



async def merchant_saas_trial_status(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, project_id: UUID | None = None,
) -> dict:
    """Account's actual immutable 72h trial claim, never a guessed start date.

    Same owner/project scope as the existing billing screen. SQL holds all
    platform financial tables behind an authorized SECURITY DEFINER RPC.
    """
    stmt = text(
        "SELECT public.platform_saas_owner_trial_status() AS data"
    ).columns(data=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id, project_id=project_id),
    ) as session:
        if project_id is not None:
            holder = await session.scalar(
                select(Project.owner_user_id).where(Project.id == project_id)
            )
            if holder != actor_user_id:
                raise PermissionError("Only project owner may view trial state")
        result = (await session.execute(stmt)).scalar_one()
    if not isinstance(result, dict) or not isinstance(result.get("state"), str):
        raise RuntimeError("Invalid ViMi Pay trial state")
    return result


async def merchant_saas_unified_monthly_preview(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, month: date,
    project_id: UUID | None = None,
) -> dict:
    """One read-only monthly preview across manual, Stars and Crypto Pay.

    Missing official rates stay visibly unresolved; receipts and paid
    purchases are never modified by a report request.
    """
    if month.day != 1:
        raise ValueError("The billing month must start on day one")
    stmt = text(
        "SELECT public.platform_saas_unified_monthly_preview("
        ":owner,:month,:project) AS data"
    ).columns(data=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id, project_id=project_id),
    ) as session:
        if project_id is not None:
            holder = await session.scalar(select(Project.owner_user_id).where(
                Project.id == project_id
            ))
            if holder != actor_user_id:
                raise PermissionError("Only project owner may read sale history")
        data = (await session.execute(stmt, {
            "owner": actor_user_id, "month": month, "project": project_id,
        })).scalar_one()
    if not isinstance(data, dict) or not isinstance(data.get("rails"), list):
        raise RuntimeError("Invalid unified ViMi Pay commission preview")
    return data


async def merchant_saas_project_preview(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, project_id: UUID, month: date,
) -> dict:
    """Read-only, owner-only preview of a connected bot's commercial sales.

    This reports the original fiat/Stars turnovers using the locked
    monthly conversion policy. No invoices, receipts or FX rows are changed.
    """
    if month.day != 1:
        raise ValueError("The accounting month must start on day 1")
    stmt = text(
        "SELECT public.platform_saas_project_preview("
        ":project,:month) AS data"
    ).columns(data=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id, project_id=project_id),
    ) as session:
        result = (await session.execute(
            stmt, {"project": project_id, "month": month},
        )).scalar_one()
    if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
        raise RuntimeError("Invalid ViMi Pay account preview")
    return result


async def merchant_saas_bill_summary(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, month: date,
    project_id: UUID | None = None,
) -> dict:
    """One read-only owner bill: native turnovers and account-wide real due.

    Connected-bot turnover is scoped to that bot; actual liabilities are
    account-wide because service and closed-month invoice allocations may
    span several bots. No issued invoices or payment attempts are changed.
    """
    if month.day != 1:
        raise ValueError("The accounting month must start on day one")
    stmt = text(
        "SELECT public.platform_saas_merchant_bill_summary("
        ":month,:project) AS data"
    ).columns(data=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id, project_id=project_id),
    ) as session:
        result = (await session.execute(stmt, {
            "month": month, "project": project_id,
        })).scalar_one()
    if not isinstance(result, dict) or not isinstance(
        result.get("turnover"), list,
    ):
        raise RuntimeError("Invalid ViMi Pay unified bill")
    return result


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
        credits = (await session.execute(
            text("SELECT public.platform_saas_refund_credit_report(:owner) AS data")
                .columns(data=JSONB),
            {"owner": actor_user_id},
        )).scalar_one()
        foreign_count = (await session.execute(
            text("SELECT public.platform_saas_owner_foreign_invoice_count()")
        )).scalar_one()
        # Runtime cannot read financial tables directly. Each invoice's
        # settlement is owner-scoped by the same audited SECURITY DEFINER RPC
        # used for its detail card.
        invoices = []
        for item in data.get("invoices", []):
            snapshot = (await session.execute(
                text(
                    "SELECT public.platform_saas_invoice_currency_snapshot("
                    ":invoice) AS snapshot"
                ).columns(snapshot=JSONB),
                {"invoice": UUID(str(item["id"]))},
            )).scalar_one()
            if not isinstance(snapshot, dict):
                raise RuntimeError("Invalid account invoice currency snapshot")
            invoices.append({**item, "settlement": snapshot})
    if not isinstance(data, dict) or not isinstance(data.get("invoices"), list):
        raise RuntimeError("Invalid merchant billing snapshot")
    if not isinstance(credits, dict):
        raise RuntimeError("Invalid merchant credit report")
    return MerchantSaasStatements(
        total=int(data["total"]),
        unpaid=int(data["unpaid"]),
        outstanding_rub=Decimal(str(data["outstanding_rub"])),
        configured=bool(data["details_configured"]),
        instructions=data["instructions"],
        invoices=tuple(invoices),
        page=page,
        pending_credit_rub=Decimal(str(credits["total_credited_rub"])),
        pending_credit_count=int(credits["credit_count"]),
        foreign_unpaid_count=int(foreign_count),
        due_now_count=int(data.get("due_now_count", data["unpaid"])),
        due_now_rub=Decimal(str(data.get("due_now_rub", data["outstanding_rub"]))),
        upcoming_count=int(data.get("upcoming_count", 0)),
        upcoming_rub=Decimal(str(data.get("upcoming_rub", "0"))),
    )
