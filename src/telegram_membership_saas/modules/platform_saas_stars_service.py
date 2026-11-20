"""Verified Telegram Stars settlement for PLATFORM invoices, not tenant sales.

Only the central ViMi Pay polling bot may pass its own Bot API pre-checkout,
successful-payment or refund updates here. PostgreSQL verifies the payer's
original owner identity and uses an independent, append-only provider ledger.

No hard-coded Stars exchange rates: only the platform operator can publish a
RUB-per-Star payout quote via the operator-only configuration RPC.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_billing_service import _check_actor
from telegram_membership_saas.tenant_context import TenantContext

STARS_CURRENCY = "XTR"  # Telegram protocol value; never display as a UI name.
STARS_LIMIT = 250_000


async def _rpc(
    factory: async_sessionmaker[AsyncSession], *,
    payer_telegram_id: int, name: str, arguments: dict,
) -> object:
    signatures = {
        "platform_saas_stars_create_order": ("invoice", "telegram_id", "bot_id"),
        "platform_saas_stars_precheckout": ("payload", "telegram_id", "bot_id", "amount"),
        "platform_saas_stars_record_charge": (
            "payload", "telegram_id", "bot_id", "amount", "charge_id", "paid_at",
        ),
        "platform_saas_stars_record_refund": (
            "payload", "telegram_id", "bot_id", "amount", "charge_id", "refunded_at",
        ),
        "platform_saas_stars_set_rate": ("rate",),
        "platform_saas_stars_rate": (),
        "platform_saas_stars_usd_policy_read": (),
        "platform_saas_stars_usd_policy_set": ("usd_per_star", "reserve_pct", "enabled"),
        "platform_saas_stars_charge_payload": ("charge_id", "telegram_id", "bot_id"),
    }
    expected = signatures.get(name)
    if expected is None or set(arguments) != set(expected):
        raise ValueError("Unsupported platform Stars operation")
    if payer_telegram_id <= 0:
        raise ValueError("Invalid Telegram identity")
    actor = await ensure_telegram_actor(
        factory, telegram_id=payer_telegram_id,
        username=None, first_name=None,
    )
    stmt = text(
        "SELECT public." + name + "("
        + ",".join(":" + key for key in expected) + ") AS data"
    )
    if name in (
        "platform_saas_stars_create_order",
        "platform_saas_stars_record_charge",
        "platform_saas_stars_usd_policy_read",
        "platform_saas_stars_usd_policy_set",
    ):
        stmt = stmt.columns(data=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor),
    ) as session:
        return (await session.execute(stmt, arguments)).scalar_one()


async def create_platform_stars_order(
    factory: async_sessionmaker[AsyncSession], *,
    invoice_id: UUID, payer_telegram_id: int, central_bot_id: int,
) -> dict:
    if central_bot_id <= 0:
        raise ValueError("Invalid central bot identity")
    data = await _rpc(
        factory, payer_telegram_id=payer_telegram_id,
        name="platform_saas_stars_create_order",
        arguments={
            "invoice": invoice_id,
            "telegram_id": payer_telegram_id,
            "bot_id": central_bot_id,
        },
    )
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("payload"), str)
        or not data["payload"].startswith("vps:")
        or not isinstance(data.get("stars_amount"), int)
        or not 1 <= data["stars_amount"] <= STARS_LIMIT
        or Decimal(str(data.get("rub_amount", "0"))) <= 0
        or Decimal(str(data.get("usd_quote", "0"))) <= 0
        or Decimal(str(data.get("usd_per_star", "0"))) <= 0
        or not 0 <= Decimal(str(data.get("withdrawal_reserve_pct", "-1"))) <= 30
    ):
        raise RuntimeError("Invalid platform Stars quote")
    return data


async def platform_stars_precheckout(
    factory: async_sessionmaker[AsyncSession], *,
    payer_telegram_id: int, central_bot_id: int, payload: str,
    currency: str, stars_amount: int,
) -> bool:
    if (
        currency != STARS_CURRENCY or not payload.startswith("vps:")
        or not 1 <= stars_amount <= STARS_LIMIT
    ):
        return False
    result = await _rpc(
        factory, payer_telegram_id=payer_telegram_id,
        name="platform_saas_stars_precheckout",
        arguments={
            "payload": payload, "telegram_id": payer_telegram_id,
            "bot_id": central_bot_id, "amount": stars_amount,
        },
    )
    return result is True


async def confirm_platform_stars_charge(
    factory: async_sessionmaker[AsyncSession], *,
    payer_telegram_id: int, central_bot_id: int,
    payload: str, currency: str, stars_amount: int,
    charge_id: str, paid_at: datetime,
) -> dict:
    if (
        currency != STARS_CURRENCY or not payload.startswith("vps:")
        or not 1 <= stars_amount <= STARS_LIMIT
        or not 8 <= len(charge_id) <= 255
        or paid_at.tzinfo is None
    ):
        raise ValueError("Invalid provider-authenticated Stars payment")
    result = await _rpc(
        factory, payer_telegram_id=payer_telegram_id,
        name="platform_saas_stars_record_charge",
        arguments={
            "payload": payload, "telegram_id": payer_telegram_id,
            "bot_id": central_bot_id, "amount": stars_amount,
            "charge_id": charge_id, "paid_at": paid_at,
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("applied"), bool):
        raise RuntimeError("Invalid verified platform Stars settlement")
    return result


async def record_platform_stars_refund(
    factory: async_sessionmaker[AsyncSession], *,
    payer_telegram_id: int, central_bot_id: int,
    payload: str, currency: str, stars_amount: int,
    charge_id: str, refunded_at: datetime,
) -> bool:
    if (
        currency != STARS_CURRENCY or not payload.startswith("vps:")
        or not 1 <= stars_amount <= STARS_LIMIT
        or not 8 <= len(charge_id) <= 255
        or refunded_at.tzinfo is None
    ):
        raise ValueError("Invalid provider-authenticated Stars refund")
    result = await _rpc(
        factory, payer_telegram_id=payer_telegram_id,
        name="platform_saas_stars_record_refund",
        arguments={
            "payload": payload, "telegram_id": payer_telegram_id,
            "bot_id": central_bot_id, "amount": stars_amount,
            "charge_id": charge_id, "refunded_at": refunded_at,
        },
    )
    return result is True


async def set_platform_stars_rate(
    factory: async_sessionmaker[AsyncSession], *,
    operator_telegram_id: int, rub_per_star: Decimal,
) -> Decimal:
    from telegram_membership_saas.config import get_settings

    _check_actor(operator_telegram_id, get_settings())
    if (
        not rub_per_star.is_finite()
        or rub_per_star < Decimal("0.01")
        or rub_per_star > Decimal("1000")
        or rub_per_star != rub_per_star.quantize(Decimal("0.00000001"))
    ):
        raise ValueError("Invalid RUB-per-Star payout rate")
    result = await _rpc(
        factory, payer_telegram_id=operator_telegram_id,
        name="platform_saas_stars_set_rate",
        arguments={"rate": rub_per_star},
    )
    return Decimal(str(result))


async def platform_stars_rate(
    factory: async_sessionmaker[AsyncSession], *,
    operator_telegram_id: int,
) -> Decimal | None:
    from telegram_membership_saas.config import get_settings

    _check_actor(operator_telegram_id, get_settings())
    result = await _rpc(
        factory, payer_telegram_id=operator_telegram_id,
        name="platform_saas_stars_rate", arguments={},
    )
    return None if result is None else Decimal(str(result))



async def recover_platform_stars_charge_payload(
    factory: async_sessionmaker[AsyncSession], *,
    payer_telegram_id: int, central_bot_id: int, charge_id: str,
) -> str | None:
    """Provider refund lookup by original *recorded* charge, never by price."""
    if not 8 <= len(charge_id) <= 255 or central_bot_id <= 0:
        raise ValueError("Invalid Telegram charge reference")
    payload = await _rpc(
        factory, payer_telegram_id=payer_telegram_id,
        name="platform_saas_stars_charge_payload",
        arguments={
            "charge_id": charge_id,
            "telegram_id": payer_telegram_id,
            "bot_id": central_bot_id,
        },
    )
    if payload is not None and (
        not isinstance(payload, str) or not payload.startswith("vps:")
    ):
        raise RuntimeError("Invalid original platform invoice payload")
    return payload


async def platform_stars_usd_policy(
    factory: async_sessionmaker[AsyncSession], *,
    operator_telegram_id: int,
) -> dict:
    """Read the approved USD reward quote. No fabricated RUB/Star FX."""
    from telegram_membership_saas.config import get_settings

    _check_actor(operator_telegram_id, get_settings())
    data = await _rpc(
        factory, payer_telegram_id=operator_telegram_id,
        name="platform_saas_stars_usd_policy_read", arguments={},
    )
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("enabled"), bool)
        or Decimal(str(data.get("usd_per_star", "0"))) <= 0
        or not 0 <= Decimal(str(data.get("withdrawal_reserve_pct", "-1"))) <= 30
        or not isinstance(data.get("example_stars"), int)
    ):
        raise RuntimeError("Invalid platform Stars USD policy")
    return data


async def configure_platform_stars_usd_policy(
    factory: async_sessionmaker[AsyncSession], *,
    operator_telegram_id: int, usd_per_star: Decimal,
    reserve_percent: Decimal, enabled: bool,
) -> dict:
    """Operator authorizes the USD quotation for *future* Stars orders."""
    from telegram_membership_saas.config import get_settings

    _check_actor(operator_telegram_id, get_settings())
    if (
        not usd_per_star.is_finite()
        or not Decimal("0.005") <= usd_per_star <= Decimal("0.05")
        or usd_per_star != usd_per_star.quantize(Decimal("0.00000001"))
        or not reserve_percent.is_finite()
        or not Decimal("0") <= reserve_percent <= Decimal("30")
        or reserve_percent != reserve_percent.quantize(Decimal("0.01"))
        or not isinstance(enabled, bool)
    ):
        raise ValueError("Invalid USD Stars reward and reserve")
    result = await _rpc(
        factory, payer_telegram_id=operator_telegram_id,
        name="platform_saas_stars_usd_policy_set",
        arguments={
            "usd_per_star": usd_per_star,
            "reserve_pct": reserve_percent,
            "enabled": enabled,
        },
    )
    if not isinstance(result, dict) or result.get("enabled") is not enabled:
        raise RuntimeError("Invalid platform Stars USD policy acknowledgement")
    return result
