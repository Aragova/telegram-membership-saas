"""One ViMi Pay commercial billing channel: accounts pay their actual invoice.

All data access uses narrow SECURITY DEFINER RPCs. Bank confirmations require
independently received funds and the platform operator's own Telegram identity.
No tenant can write platform ledgers, edit a receipt or change customer access.
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

PAYMENT_CURRENCIES = frozenset(("RUB", "USD", "EUR", "UAH", "BYN"))


async def _rpc(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID | None = None,
    name: str, arguments: dict,
) -> object:
    """Named, closed-set SQL entrypoints, never interpolate a caller's name."""
    names = {
        "platform_saas_v2_invoice_balance": ("invoice",),
        "platform_saas_v2_available_methods": ("invoice",),
        "platform_saas_v2_methods_read": (),
        "platform_saas_v2_create_attempt": ("invoice", "amount_rub", "currency"),
        "platform_saas_v2_submit_proof":
            ("attempt", "bot_id", "message_id", "file_id", "file_type"),
        "platform_saas_v2_bank_method": ("currency", "instructions"),
        "platform_saas_v2_approve_attempt": ("attempt", "bank_reference"),
        "platform_saas_v2_reject_attempt": ("attempt", "reason"),
        "platform_saas_v2_pending_proofs": ("page",),
    }
    keys = names.get(name)
    if keys is None or set(arguments) != set(keys):
        raise ValueError("Unknown ViMi Pay billing operation")
    query = text(
        "SELECT public." + name + "("
        + ",".join(":" + key for key in keys) + ") AS data"
    )
    if name in {
        "platform_saas_v2_invoice_balance",
        "platform_saas_v2_available_methods",
        "platform_saas_v2_methods_read",
        "platform_saas_v2_create_attempt",
        "platform_saas_v2_pending_proofs",
    }:
        query = query.columns(data=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        return (await session.execute(query, arguments)).scalar_one()


async def saas_v2_available_methods(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, invoice_id: UUID, project_id: UUID | None = None,
) -> tuple[str, ...]:
    data = await _rpc(
        factory, actor_id=actor_id, project_id=project_id,
        name="platform_saas_v2_available_methods",
        arguments={"invoice": invoice_id},
    )
    if not isinstance(data, list) or any(
        not isinstance(code, str) or code not in PAYMENT_CURRENCIES
        for code in data
    ):
        raise RuntimeError("Invalid platform bank currency list")
    return tuple(data)


async def saas_v2_invoice_balance(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, invoice_id: UUID, project_id: UUID | None = None,
) -> dict:
    result = await _rpc(
        factory, actor_id=actor_id, project_id=project_id,
        name="platform_saas_v2_invoice_balance",
        arguments={"invoice": invoice_id},
    )
    if not isinstance(result, dict) or "remaining_rub" not in result:
        raise RuntimeError("Invalid account invoice balance")
    return result


async def saas_v2_create_attempt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, invoice_id: UUID, rub: Decimal, currency: str,
    project_id: UUID | None = None,
) -> dict:
    if currency not in PAYMENT_CURRENCIES or not rub.is_finite() or rub <= 0:
        raise ValueError("Invalid partial payment currency/amount")
    if rub != rub.quantize(Decimal("0.01")):
        raise ValueError("Partial payment amount must have cents precision")
    result = await _rpc(
        factory, actor_id=actor_id, project_id=project_id,
        name="platform_saas_v2_create_attempt",
        arguments={"invoice": invoice_id, "amount_rub": rub, "currency": currency},
    )
    if not isinstance(result, dict) or "id" not in result:
        raise RuntimeError("Invalid quoted bank payment attempt")
    return result


async def saas_v2_submit_proof(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, attempt_id: UUID, project_id: UUID,
    bot_id: int, message_id: int, file_id: str, file_type: str,
) -> UUID:
    if not project_id or bot_id <= 0 or message_id <= 0:
        raise PermissionError("Connected bot source required")
    if file_type not in ("photo", "document") or not file_id:
        raise ValueError("Unsupported payment evidence")
    result = await _rpc(
        factory, actor_id=actor_id, project_id=project_id,
        name="platform_saas_v2_submit_proof",
        arguments={
            "attempt": attempt_id, "bot_id": bot_id,
            "message_id": message_id, "file_id": file_id,
            "file_type": file_type,
        },
    )
    return UUID(str(result))


async def saas_v2_methods_read(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, actor_telegram_id: int,
    settings: Settings | None = None,
) -> tuple[dict, ...]:
    _check_actor(actor_telegram_id, settings)
    result = await _rpc(
        factory, actor_id=actor_id,
        name="platform_saas_v2_methods_read", arguments={},
    )
    if not isinstance(result, list):
        raise RuntimeError("Invalid ViMi Pay method catalog")
    return tuple(result)


async def saas_v2_configure_method(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, actor_telegram_id: int, currency: str,
    instructions: str | None, settings: Settings | None = None,
) -> bool:
    _check_actor(actor_telegram_id, settings)
    if currency not in PAYMENT_CURRENCIES:
        raise ValueError("Unknown platform payment currency")
    return bool(await _rpc(
        factory, actor_id=actor_id, name="platform_saas_v2_bank_method",
        arguments={"currency": currency, "instructions": instructions},
    ))


async def saas_v2_pending_proofs(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, actor_telegram_id: int, page: int = 0,
    settings: Settings | None = None,
) -> dict:
    _check_actor(actor_telegram_id, settings)
    if not 0 <= page <= 100000:
        raise ValueError("Invalid payment review page")
    result = await _rpc(
        factory, actor_id=actor_id,
        name="platform_saas_v2_pending_proofs",
        arguments={"page": page},
    )
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise RuntimeError("Invalid bank review queue")
    return result


async def saas_v2_confirm_attempt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, actor_telegram_id: int,
    attempt_id: UUID, verified_bank_reference: str,
    settings: Settings | None = None,
) -> UUID:
    _check_actor(actor_telegram_id, settings)
    if not 8 <= len(verified_bank_reference.strip()) <= 160:
        raise ValueError("Independently verified bank reference required")
    return UUID(str(await _rpc(
        factory, actor_id=actor_id,
        name="platform_saas_v2_approve_attempt",
        arguments={
            "attempt": attempt_id,
            "bank_reference": verified_bank_reference.strip(),
        },
    )))


async def saas_v2_reject_attempt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, actor_telegram_id: int,
    attempt_id: UUID, reason: str,
    settings: Settings | None = None,
) -> UUID:
    _check_actor(actor_telegram_id, settings)
    if not 5 <= len(reason.strip()) <= 500:
        raise ValueError("Review reason required")
    return UUID(str(await _rpc(
        factory, actor_id=actor_id,
        name="platform_saas_v2_reject_attempt",
        arguments={"attempt": attempt_id, "reason": reason.strip()},
    )))
