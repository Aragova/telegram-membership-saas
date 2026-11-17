"""Original-invoice ViMi Pay settlement for owner-owned external gateways.

Only the CENTRAL bot creates payment orders; merchants never supply provider
keys and an external paid response never changes the ledger without the
platform-owner RPC. All JSON RPCs enforce original liability, frozen RUB
amount, immutable provider identifiers and exactly-once application in SQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.crypto_pay.client import CryptoPayError
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_crypto_client import (
    create_platform_crypto_invoice,
    lookup_platform_crypto_payment,
)
from telegram_membership_saas.modules.platform_saas_lava_client import (
    LavaTopClient,
    LavaTopError,
    verify_lava_paid_invoice,
)
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class GatewayLink:
    provider: str
    order_id: UUID
    invoice_url: str
    amount_rub: Decimal


async def _rpc(
    factory: async_sessionmaker[AsyncSession], *,
    telegram_id: int, name: str, values: dict,
    as_json: bool,
) -> object:
    signatures = {
        "platform_saas_gateway_reserve":
            ("invoice", "provider", "telegram_id", "bot_id"),
        "platform_saas_gateway_bind":
            ("order", "telegram_id", "bot_id", "provider_id", "url"),
        "platform_saas_gateway_pending": ("limit",),
        "platform_saas_gateway_record_charge":
            ("order", "provider", "provider_id", "amount", "paid_at"),
    }
    sig = signatures.get(name)
    if sig is None or set(values) != set(sig) or telegram_id <= 0:
        raise ValueError("Unsupported platform settlement operation")
    actor = await ensure_telegram_actor(
        factory, telegram_id=telegram_id,
        username=None, first_name=None,
    )
    query = text(
        "SELECT public." + name + "("
        + ",".join(":" + key for key in sig) + ") AS result"
    )
    if as_json:
        query = query.columns(result=JSONB)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor),
    ) as session:
        return (await session.execute(query, values)).scalar_one()


def _central_only(settings: Settings, *, bot_id: int) -> None:
    if settings.telegram_central_access_enabled or bot_id <= 0:
        raise PermissionError("External ViMi Pay billing requires the central bot")


def _platform_operator(settings: Settings) -> int:
    if (
        settings.telegram_central_access_enabled
        or settings.platform_owner_telegram_id is None
        or settings.platform_owner_telegram_id <= 0
    ):
        raise PermissionError("Central platform owner is not configured")
    return settings.platform_owner_telegram_id


async def _reserve(
    factory: async_sessionmaker[AsyncSession], *,
    invoice_id: UUID, provider: str, payer_telegram_id: int,
    bot_id: int,
) -> dict:
    result = await _rpc(
        factory, telegram_id=payer_telegram_id,
        name="platform_saas_gateway_reserve",
        values={
            "invoice": invoice_id, "provider": provider,
            "telegram_id": payer_telegram_id, "bot_id": bot_id,
        }, as_json=True,
    )
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("id"), str)
        or result.get("status") not in ("issued", "reserved")
        or Decimal(str(result.get("amount_rub", "0"))) <= 0
    ):
        raise RuntimeError("Invalid platform gateway reservation")
    return result


async def _bind(
    factory: async_sessionmaker[AsyncSession], *,
    order_id: UUID, payer_telegram_id: int, bot_id: int,
    provider_id: str, url: str,
) -> None:
    result = await _rpc(
        factory, telegram_id=payer_telegram_id,
        name="platform_saas_gateway_bind",
        values={
            "order": order_id, "telegram_id": payer_telegram_id,
            "bot_id": bot_id, "provider_id": provider_id,
            "url": url,
        }, as_json=False,
    )
    if result is not True:
        raise RuntimeError("Provider invoice was not bound to its original order")


async def issue_platform_crypto_checkout(
    factory: async_sessionmaker[AsyncSession], *,
    settings: Settings, payer_telegram_id: int,
    central_bot_id: int, invoice_id: UUID,
) -> GatewayLink:
    _central_only(settings, bot_id=central_bot_id)
    if settings.saas_crypto_pay_api_token is None:
        raise CryptoPayError("Platform Crypto Pay credentials not configured")
    reservation = await _reserve(
        factory, invoice_id=invoice_id, provider="crypto",
        payer_telegram_id=payer_telegram_id, bot_id=central_bot_id,
    )
    order_id = UUID(str(reservation["id"]))
    amount = Decimal(str(reservation["amount_rub"]))
    if reservation["status"] == "issued":
        url = reservation.get("url")
        if not isinstance(url, str) or not url.startswith("https://t.me/"):
            raise RuntimeError("Original Crypto Pay invoice link is unavailable")
        return GatewayLink("crypto", order_id, url, amount)
    provider = await create_platform_crypto_invoice(
        settings, order_id=order_id, rub_amount=amount,
    )
    await _bind(
        factory, order_id=order_id,
        payer_telegram_id=payer_telegram_id,
        bot_id=central_bot_id,
        provider_id=str(provider.provider_invoice_id),
        url=provider.bot_invoice_url,
    )
    return GatewayLink("crypto", order_id, provider.bot_invoice_url, amount)


async def issue_platform_lava_checkout(
    factory: async_sessionmaker[AsyncSession], *,
    settings: Settings, payer_telegram_id: int, central_bot_id: int,
    invoice_id: UUID, customer_email: str,
) -> GatewayLink:
    """Email is supplied by the real payer; no fictitious email is invented."""
    _central_only(settings, bot_id=central_bot_id)
    api_key = settings.saas_lava_top_api_key
    offer = settings.saas_lava_top_offer_id
    if api_key is None or offer is None:
        raise LavaTopError("Platform Lava.top credentials/product not configured")
    reservation = await _reserve(
        factory, invoice_id=invoice_id, provider="lava",
        payer_telegram_id=payer_telegram_id, bot_id=central_bot_id,
    )
    order_id = UUID(str(reservation["id"]))
    amount = Decimal(str(reservation["amount_rub"]))
    if reservation["status"] == "issued":
        url = reservation.get("url")
        if not isinstance(url, str) or "lava.top" not in url:
            raise RuntimeError("Original Lava.top checkout link is unavailable")
        return GatewayLink("lava", order_id, url, amount)
    async with LavaTopClient(api_key.get_secret_value()) as provider:
        created = await provider.create_dynamic_invoice(
            email=customer_email, offer_id=offer,
            amount=amount, currency="RUB",
        )
    await _bind(
        factory, order_id=order_id,
        payer_telegram_id=payer_telegram_id,
        bot_id=central_bot_id,
        provider_id=str(created.invoice_id),
        url=created.payment_url,
    )
    return GatewayLink("lava", order_id, created.payment_url, amount)


async def list_platform_pending_gateway_orders(
    factory: async_sessionmaker[AsyncSession], *,
    settings: Settings, limit: int = 100,
) -> tuple[dict, ...]:
    result = await _rpc(
        factory, telegram_id=_platform_operator(settings),
        name="platform_saas_gateway_pending",
        values={"limit": limit}, as_json=True,
    )
    if not isinstance(result, list):
        raise RuntimeError("Invalid pending platform gateway report")
    return tuple(result)


async def verify_and_record_platform_gateway_payment(
    factory: async_sessionmaker[AsyncSession], *,
    settings: Settings, pending: dict,
) -> dict | None:
    """Provider GET on platform-owned token precedes all SQL money effects."""
    provider_name = pending.get("provider")
    frozen_amount = Decimal(str(pending["amount_rub"]))
    provider_id = str(pending["provider_invoice_id"])
    order_id = UUID(str(pending["id"]))
    payload = str(pending["invoice_payload"])
    if provider_name == "crypto":
        if settings.saas_crypto_pay_api_token is None:
            raise CryptoPayError("Missing owner Crypto Pay credentials")
        paid = await lookup_platform_crypto_payment(
            settings, provider_invoice_id=int(provider_id),
            original_payload=payload, rub_amount=frozen_amount,
        )
        if paid is None:
            return None
        stamp: datetime = paid.paid_at
    elif provider_name == "lava":
        api_key = settings.saas_lava_top_api_key
        if api_key is None:
            raise LavaTopError("Missing owner Lava.top credentials")
        async with LavaTopClient(api_key.get_secret_value()) as client:
            snapshot = await client.get_invoice(invoice_id=UUID(provider_id))
        # The authoritative GET is matched against the ORIGINAL provider
        # invoice ID, RUB currency and exact immutable local amount.
        verify_lava_paid_invoice(
            snapshot, expected_id=UUID(provider_id),
            expected_currency="RUB", expected_amount=frozen_amount,
        )
        # A provider-specific paid timestamp is required before accounting.
        raw_date = snapshot.get("paidAt")
        if not isinstance(raw_date, str):
            raise LavaTopError("Missing provider-verified paid timestamp")
        stamp = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise LavaTopError("Invalid provider paid timestamp")
    else:
        raise ValueError("Invalid platform provider")
    recorded = await _rpc(
        factory, telegram_id=_platform_operator(settings),
        name="platform_saas_gateway_record_charge",
        values={
            "order": order_id, "provider": provider_name,
            "provider_id": provider_id,
            "amount": frozen_amount, "paid_at": stamp,
        }, as_json=True,
    )
    if not isinstance(recorded, dict) or not isinstance(recorded.get("applied"), bool):
        raise RuntimeError("Invalid platform gateway settlement")
    return recorded
