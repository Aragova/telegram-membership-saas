"""Dormant, narrow RPC access for issued-currency quotes and refund evidence.

These methods are not wired to a new Telegram menu or automated billing.
PostgreSQL re-checks the actual liable account and platform-owner identity.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.platform_saas_billing_service import _check_actor
from telegram_membership_saas.tenant_context import TenantContext

SETTLEMENT_CURRENCIES = frozenset(("RUB", "USD", "EUR", "UAH", "BYN"))
CREDIT_REASONS = frozenset(("refund", "chargeback", "correction"))


async def select_saas_invoice_currency(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    invoice_id: UUID,
    currency: str,
) -> dict:
    """The *liable merchant owner* freezes one quote for an issued invoice.

    Repeated selection of the same currency returns the ORIGINAL snapshot,
    not a new rate. The first selection cannot be changed later.
    """
    if currency not in SETTLEMENT_CURRENCIES:
        raise ValueError("Unsupported SaaS settlement currency")
    stmt = text(
        "SELECT public.platform_saas_select_invoice_currency("
        ":invoice,:currency) AS snapshot"
    ).columns(snapshot=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        result = (await session.execute(stmt, {
            "invoice": invoice_id, "currency": currency,
        })).scalar_one()
    if not isinstance(result, dict) or result.get("currency") != currency:
        raise RuntimeError("Invalid currency settlement snapshot")
    return result


async def saas_invoice_currency_snapshot(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    invoice_id: UUID,
) -> dict:
    """Read-only, owner-scoped view. Never reveals another account's bill."""
    stmt = text(
        "SELECT public.platform_saas_invoice_currency_snapshot(:invoice)"
        " AS snapshot"
    ).columns(snapshot=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        result = (await session.execute(
            stmt, {"invoice": invoice_id},
        )).scalar_one()
    if not isinstance(result, dict):
        raise RuntimeError("Invalid frozen settlement view")
    return result


async def confirm_saas_currency_receipt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    invoice_id: UUID,
    currency: str,
    amount: Decimal,
    reference: str | None = None,
    settings: Settings | None = None,
) -> UUID:
    """Platform owner only, after verifying the actual bank receipt.

    This is NOT a merchant-side 'I paid' action. The exact received currency
    and value must match the one immutably published on the issued invoice.
    """
    _check_actor(actor_telegram_id, settings)
    if currency not in SETTLEMENT_CURRENCIES:
        raise ValueError("Unsupported payment currency")
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(
        Decimal("0.01")
    ):
        raise ValueError("Invalid received amount")
    if reference is not None and (
        not reference.strip() or len(reference) > 160
    ):
        raise ValueError("Invalid payment reference")
    stmt = text(
        "SELECT public.platform_saas_confirm_currency_receipt("
        ":invoice,:currency,:amount,:reference)"
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        return (await session.execute(stmt, {
            "invoice": invoice_id, "currency": currency,
            "amount": amount, "reference": reference,
        })).scalar_one()


async def record_saas_refund_credit(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    actor_telegram_id: int,
    invoice_line_id: UUID,
    refund_reference: str,
    credit_rub: Decimal,
    reason: str,
    settings: Settings | None = None,
) -> UUID:
    """Audit one independently verified refund or chargeback against a sale.

    This does not refund a buyer or automatically offset a pending invoice.
    """
    _check_actor(actor_telegram_id, settings)
    if reason not in CREDIT_REASONS:
        raise ValueError("Invalid refund reason")
    if not 8 <= len(refund_reference.strip()) <= 160:
        raise ValueError("Refund reference must have 8 to 160 characters")
    if not credit_rub.is_finite() or credit_rub <= 0 or credit_rub != (
        credit_rub.quantize(Decimal("0.01"))
    ):
        raise ValueError("Invalid refund credit")
    stmt = text(
        "SELECT public.platform_saas_record_refund_credit("
        ":line,:reference,:credit,:reason)"
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        return (await session.execute(stmt, {
            "line": invoice_line_id, "reference": refund_reference.strip(),
            "credit": credit_rub, "reason": reason,
        })).scalar_one()


async def saas_refund_credit_report(
    factory: async_sessionmaker[AsyncSession], *,
    actor_user_id: UUID,
    owner_user_id: UUID,
) -> dict:
    """Account owner or verified platform operator views pending credits."""
    stmt = text(
        "SELECT public.platform_saas_refund_credit_report(:owner) AS data"
    ).columns(data=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_user_id),
    ) as session:
        data = (await session.execute(
            stmt, {"owner": owner_user_id},
        )).scalar_one()
    if not isinstance(data, dict):
        raise RuntimeError("Invalid credit report")
    return data
