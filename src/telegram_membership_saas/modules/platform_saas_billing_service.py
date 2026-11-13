"""Owner-authorized ViMi Pay SaaS billing operations.

All SQL entry points re-check the provisioned platform owner in PostgreSQL.
The billing master switch defaults to disabled, and normal bot handlers never
invoke invoice issuing. Manual receipt approval records verified owner action,
not a customer's unverified "I paid" click.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.platform_dashboard import (
    is_configured_platform_owner,
)
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class SaasInvoiceSnapshot:
    enabled: bool
    holds_enabled: bool
    grace_days: int
    held_owners: int
    grace_owners: int
    issued: int
    unpaid: int
    unpaid_rub: Decimal
    received_rub: Decimal
    page: int
    pages: int
    invoices: tuple[dict, ...]
    due_now_count: int = 0
    due_now_rub: Decimal = Decimal("0")
    upcoming_count: int = 0
    upcoming_rub: Decimal = Decimal("0")


def _check_actor(actor_telegram_id: int, settings: Settings | None) -> None:
    if not is_configured_platform_owner(actor_telegram_id, settings or get_settings()):
        raise PermissionError("Platform SaaS billing is owner-only")


async def commercial_release_status(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, actor_telegram_id: int,
    settings: Settings | None = None,
) -> dict:
    """Actual commercial billing state of the connected network."""
    _check_actor(actor_telegram_id, settings)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        result = (await session.execute(text(
            "SELECT public.platform_saas_release_status() AS state"
        ).columns(state=JSONB))).scalar_one()
    if not isinstance(result, dict):
        raise RuntimeError("Commercial billing status unavailable")
    return result


async def commercial_release_change(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID, actor_telegram_id: int,
    action: str, expected_bots: int, expected_other_owners: int,
    settings: Settings | None = None,
) -> dict:
    """Owner's explicit production activation or emergency pause.

    PostgreSQL verifies actual inventory in the same atomic transaction,
    rather than trusting counters supplied by the Telegram callback.
    """
    _check_actor(actor_telegram_id, settings)
    if action not in {"arm", "stop"}:
        raise ValueError("Unsupported release control")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        result = (await session.execute(text(
            "SELECT public.platform_saas_release_change("
            ":action,:bots,:other_owners) AS state"
        ).columns(state=JSONB), {
            "action": action, "bots": expected_bots,
            "other_owners": expected_other_owners,
        })).scalar_one()
    if not isinstance(result, dict):
        raise RuntimeError("Commercial billing activation failed")
    return result


async def platform_saas_invoice_report(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    page: int = 0,
    settings: Settings | None = None,
) -> SaasInvoiceSnapshot:
    _check_actor(actor_telegram_id, settings)
    if not 0 <= page <= 100000:
        raise ValueError("Invalid SaaS invoice page")
    stmt = text(
        "SELECT public.platform_saas_hold_invoice_report(:page, :size) AS snapshot"
    ).columns(snapshot=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        data = (await session.execute(stmt, {"page": page, "size": 6})).scalar_one()
    if not isinstance(data, dict) or not isinstance(data.get("invoices"), list):
        raise RuntimeError("Invalid SaaS invoice report")
    return SaasInvoiceSnapshot(
        enabled=bool(data["enabled"]),
        holds_enabled=bool(data["holds_enabled"]),
        grace_days=int(data["grace_days"]),
        held_owners=int(data["held_owners"]),
        grace_owners=int(data["grace_owners"]),
        issued=int(data["total"]),
        unpaid=int(data["unpaid"]),
        unpaid_rub=Decimal(str(data["unpaid_rub"])),
        received_rub=Decimal(str(data["received_rub"])),
        page=page,
        pages=int(data["pages"]),
        invoices=tuple(data["invoices"]),
        due_now_count=int(data.get("due_now_count", data["unpaid"])),
        due_now_rub=Decimal(str(data.get("due_now_rub", data["unpaid_rub"]))),
        upcoming_count=int(data.get("upcoming_count", 0)),
        upcoming_rub=Decimal(str(data.get("upcoming_rub", "0"))),
    )


async def issue_saas_bot_prepayment(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    bot_id: UUID,
    period_start: datetime,
    period_end: datetime,
    settings: Settings | None = None,
) -> UUID | None:
    """Issue idempotent RUB 150 service invoice, once the policy is activated."""
    _check_actor(actor_telegram_id, settings)
    stmt = text(
        "SELECT public.platform_saas_issue_bot_invoice("
        ":bot,:start,:end)"
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        return (await session.execute(stmt, {
            "bot": bot_id, "start": period_start, "end": period_end,
        })).scalar_one()


async def issue_saas_monthly_commission(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    merchant_id: UUID,
    owner_user_id: UUID,
    billing_month: date,
    rub_per_unit: dict[str, Decimal],
    settings: Settings | None = None,
) -> UUID | None:
    """Issue 5% on approved subscription and manual-file purchases only.

    Each foreign sale is converted using its published Moscow payment-day
    rate. The legacy currency map must be empty; DB snapshots the FX.
    PostgreSQL verifies every source and historical bot attribution.
    """
    _check_actor(actor_telegram_id, settings)
    if billing_month.day != 1:
        raise ValueError("The billing period must start on day one")
    if rub_per_unit:
        # The old monthly override is deliberately unsupported. Publishing
        # an immutable quote for each Moscow payment date is required.
        raise ValueError(
            "Monthly FX overrides are disabled; use published daily rates"
        )
    data = "{}"
    stmt = text(
        "SELECT public.platform_saas_issue_commission_invoice("
        ":merchant,:owner,:month,CAST(:fx AS jsonb))"
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        return (await session.execute(stmt, {
            "merchant": merchant_id, "owner": owner_user_id,
            "month": billing_month, "fx": data,
        })).scalar_one()


async def confirm_saas_receipt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    invoice_id: UUID,
    reference: str | None = None,
    settings: Settings | None = None,
) -> UUID:
    """Owner confirms verified receipt on the bank side. Never automatic."""
    _check_actor(actor_telegram_id, settings)
    stmt = text("SELECT public.platform_saas_confirm_receipt(:id,:reference)")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        return (await session.execute(stmt, {
            "id": invoice_id, "reference": reference,
        })).scalar_one()
