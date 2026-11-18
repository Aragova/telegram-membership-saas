"""Original-invoice ViMi Pay settlement for owner-owned external gateways.

Only the CENTRAL bot creates payment orders; merchants never supply provider
keys and an external paid response never changes the ledger without the
platform-owner RPC. All JSON RPCs enforce original liability, frozen RUB
amount, immutable provider identifiers and exactly-once application in SQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.crypto_pay.client import (
    CryptoPayError,
    _invoice_link,
)
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_crypto_client import (
    create_platform_crypto_invoice,
    lookup_platform_crypto_payment,
)
from telegram_membership_saas.modules.platform_saas_lava_client import (
    LavaTopClient,
    LavaTopError,
    _checkout_link,
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
        "platform_saas_gateway_operator_refund":
            ("provider", "provider_id", "refund_id", "amount"),
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
    settings: Settings, order_id: UUID, payer_telegram_id: int,
    bot_id: int, provider_id: str, url: str,
) -> None:
    result = await _rpc(
        factory, telegram_id=_platform_operator(settings),
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
        if not isinstance(url, str):
            raise RuntimeError("Original Crypto Pay invoice link is unavailable")
        return GatewayLink(
            "crypto", order_id,
            _invoice_link(url, testnet=settings.saas_crypto_pay_testnet),
            amount,
        )
    provider = await create_platform_crypto_invoice(
        settings, order_id=order_id, rub_amount=amount,
    )
    await _bind(
        factory, settings=settings, order_id=order_id,
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
    try:
        reservation = await _reserve(
            factory, invoice_id=invoice_id, provider="lava",
            payer_telegram_id=payer_telegram_id, bot_id=central_bot_id,
        )
    except Exception:
        # Preserve the frozen original invoice; report only stage, not the
        # provider order, payer identity, DB error text or customer email.
        raise LavaTopError(
            "Original invoice could not be reserved",
            code="LAVA-RESERVE",
        ) from None
    order_id = UUID(str(reservation["id"]))
    amount = Decimal(str(reservation["amount_rub"]))
    if reservation["status"] == "issued":
        url = reservation.get("url")
        return GatewayLink(
            "lava", order_id, _checkout_link(url), amount,
        )
    try:
        async with LavaTopClient(api_key.get_secret_value()) as provider:
            created = await provider.create_dynamic_invoice(
                email=customer_email, offer_id=offer,
                amount=amount, currency="RUB",
            )
    except LavaTopError:
        raise
    except Exception:
        raise LavaTopError(
            "Lava.top invoice creation unavailable", code="LAVA-CREATE",
        ) from None
    try:
        await _bind(
            factory, settings=settings, order_id=order_id,
            payer_telegram_id=payer_telegram_id,
            bot_id=central_bot_id,
            provider_id=str(created.invoice_id),
            url=created.payment_url,
        )
    except Exception:
        # The provider MAY have issued a link; do not claim payment success
        # or change the original invoice balance if binding failed.
        raise LavaTopError(
            "Lava.top provider invoice binding failed", code="LAVA-BIND",
        ) from None
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
        # Published Lava GET returns status=completed and receipt with
        # original amount/currency. Its datetime is invoice creation time,
        # NOT a verified paid_at. Record verified observation time only.
        if snapshot.get("status") != "completed":
            return None
        verify_lava_paid_invoice(
            snapshot, expected_id=UUID(provider_id),
            expected_currency="RUB", expected_amount=frozen_amount,
        )
        stamp = datetime.now(UTC)
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



async def record_operator_verified_gateway_refund(
    factory: async_sessionmaker[AsyncSession], *,
    settings: Settings, provider: str, provider_invoice_id: str,
    provider_refund_id: str, amount_rub: Decimal,
) -> dict:
    """Log a verified outbound refund using the central OWNER identity.

    The operator must first verify the actual provider transaction in their
    own Lava/Crypto Pay control panel. This is NOT provider-side refund API
    issuance, payment discovery or an unsourced claim that money was returned.
    Both Python and SECURITY DEFINER SQL require the platform owner.
    """
    if provider not in ("lava", "crypto"):
        raise ValueError("Unsupported provider for verified refund")
    if (
        not isinstance(provider_invoice_id, str)
        or not 1 <= len(provider_invoice_id) <= 128
        or not isinstance(provider_refund_id, str)
        or not 1 <= len(provider_refund_id) <= 128
        or any(ch.isspace() for ch in provider_invoice_id + provider_refund_id)
        or not isinstance(amount_rub, Decimal)
        or not amount_rub.is_finite()
        or amount_rub <= 0
        or amount_rub != amount_rub.quantize(Decimal("0.01"))
    ):
        raise ValueError("Invalid externally verified refund evidence")
    result = await _rpc(
        factory, telegram_id=_platform_operator(settings),
        name="platform_saas_gateway_operator_refund",
        values={
            "provider": provider, "provider_id": provider_invoice_id,
            "refund_id": provider_refund_id, "amount": amount_rub,
        }, as_json=True,
    )
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("recorded"), bool)
        or not isinstance(result.get("amount_rub"), str)
        or not isinstance(result.get("credit_reversed_rub"), str)
    ):
        raise RuntimeError("Provider refund ledger acknowledgement invalid")
    return result
