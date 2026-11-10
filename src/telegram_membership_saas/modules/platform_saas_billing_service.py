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
from json import dumps
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
    issued: int
    unpaid: int
    unpaid_rub: Decimal
    received_rub: Decimal
    page: int
    pages: int
    invoices: tuple[dict, ...]


def _check_actor(actor_telegram_id: int, settings: Settings | None) -> None:
    if not is_configured_platform_owner(actor_telegram_id, settings or get_settings()):
        raise PermissionError("Platform SaaS billing is owner-only")


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
        "SELECT public.platform_saas_invoice_report(:page, :size) AS snapshot"
    ).columns(snapshot=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        data = (await session.execute(stmt, {"page": page, "size": 6})).scalar_one()
    if not isinstance(data, dict) or not isinstance(data.get("invoices"), list):
        raise RuntimeError("Invalid SaaS invoice report")
    return SaasInvoiceSnapshot(
        enabled=bool(data["enabled"]),
        issued=int(data["total"]),
        unpaid=int(data["unpaid"]),
        unpaid_rub=Decimal(str(data["unpaid_rub"])),
        received_rub=Decimal(str(data["received_rub"])),
        page=page,
        pages=int(data["pages"]),
        invoices=tuple(data["invoices"]),
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

    All conversion rates must be configured per currency for this month.
    PostgreSQL verifies each purchase snapshot and immutable source bot.
    """
    _check_actor(actor_telegram_id, settings)
    if billing_month.day != 1:
        raise ValueError("The billing period must start on day one")
    rates = {}
    for currency, rate in rub_per_unit.items():
        if not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0:
            raise ValueError("FX rates must be positive finite Decimals")
        if currency != currency.upper() or not currency.isalpha():
            raise ValueError("Use uppercase currency codes")
        rates[currency] = str(rate)
    # Validate input in Python; SQL repeats authorization and source checks.
    # jsonb numeric values are safe and exact for numeric conversions.
    data = dumps({k: Decimal(v) and float(v) for k, v in rates.items()})
    # Decimal -> float risks lost precision: use literal numeric JSON tokens.
    data = "{" + ",".join(
        dumps(k) + ":" + str(Decimal(v)) for k, v in rates.items()
    ) + "}"
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
