"""Isolated ViMi Pay *platform* Crypto Pay app, never tenant credentials.

Strict provider I/O and frozen quote verification only. A paid provider
invoice is NOT by itself a settlement: the original SaaS invoice/owner and
atomic idempotent database ledger must still accept the charge. No financial
state is modified from an unverified callback or return URL.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.crypto_pay.client import (
    CryptoPayClient,
    CryptoPayError,
    CryptoPayInvoiceReceipt,
    verify_created_invoice_snapshot,
    verify_paid_invoice,
)

SaaS_CRYPTO_PREFIX = "vpc:"
RUB_UNIT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class PlatformCryptoInvoice:
    provider_invoice_id: int
    bot_invoice_url: str
    rub_amount: Decimal
    original_payload: str


def _validate_platform_quote(*, payload: str, rub_amount: Decimal) -> None:
    if (
        not isinstance(payload, str) or not payload.startswith(SaaS_CRYPTO_PREFIX)
        or len(payload.encode("utf-8")) > 128
    ):
        raise ValueError("Invalid platform billing checkout payload")
    try:
        UUID(payload[len(SaaS_CRYPTO_PREFIX):])
    except ValueError as exc:
        raise ValueError("Invalid platform billing order ID") from exc
    if (
        not isinstance(rub_amount, Decimal) or not rub_amount.is_finite()
        or rub_amount < RUB_UNIT or rub_amount != rub_amount.quantize(RUB_UNIT)
    ):
        raise ValueError("Invalid frozen RUB invoice balance")


def _platform_client(
    settings: Settings, *,
    transport=None,
) -> CryptoPayClient:
    """Only the configured central app may receive platform service funds."""
    if settings.telegram_central_access_enabled:
        raise PermissionError("Platform Crypto Pay is central-bot-only")
    if (
        settings.environment == "production"
        and settings.saas_crypto_pay_testnet
    ):
        raise PermissionError("Testnet cannot settle live ViMi Pay invoices")
    secret = settings.saas_crypto_pay_api_token
    if secret is None or not secret.get_secret_value().strip():
        raise CryptoPayError("Platform Crypto Pay is not configured")
    return CryptoPayClient(
        secret.get_secret_value(),
        testnet=settings.saas_crypto_pay_testnet,
        transport=transport,
    )


async def create_platform_crypto_invoice(
    settings: Settings, *,
    order_id: UUID, rub_amount: Decimal,
    transport=None,
) -> PlatformCryptoInvoice:
    """Issue provider fiat RUB invoice for an already-reserved local order.

    A committed order reservation must precede this API call. Never publish
    the returned link until the provider ID is committed against that order.
    """
    payload=SaaS_CRYPTO_PREFIX + str(order_id)
    _validate_platform_quote(payload=payload, rub_amount=rub_amount)
    async with _platform_client(settings, transport=transport) as provider:
        data = await provider.create_invoice(
            currency="RUB", amount=rub_amount, payload=payload,
            description="ViMi Pay · bot service", expires_in=1800,
        )
    identity, link = verify_created_invoice_snapshot(
        data, payload=payload, amount=rub_amount,
        currency_code="RUB", currency_type="fiat",
        testnet=settings.saas_crypto_pay_testnet,
    )
    if link is None:
        raise CryptoPayError("Platform Crypto Pay invoice has no payable link")
    return PlatformCryptoInvoice(
        provider_invoice_id=identity, bot_invoice_url=link,
        rub_amount=rub_amount, original_payload=payload,
    )


async def lookup_platform_crypto_payment(
    settings: Settings, *,
    provider_invoice_id: int, original_payload: str,
    rub_amount: Decimal, transport=None,
) -> CryptoPayInvoiceReceipt | None:
    """Only exact, originally reserved provider invoice can settle the order.

    Caller must match to the frozen local DB record and perform an atomic
    charge insert/activation. An absent or active invoice is *not paid*.
    """
    _validate_platform_quote(
        payload=original_payload, rub_amount=rub_amount,
    )
    if type(provider_invoice_id) is not int or provider_invoice_id <= 0:
        raise ValueError("Invalid provider invoice ID")
    async with _platform_client(settings, transport=transport) as provider:
        items = await provider.get_invoices((provider_invoice_id,))
    if len(items) != 1:
        return None
    data = items[0]
    if data.get("status") in ("active", "expired"):
        return None
    return verify_paid_invoice(
        data, invoice_id=provider_invoice_id,
        payload=original_payload, amount=rub_amount,
        currency_code="RUB", currency_type="fiat",
    )
