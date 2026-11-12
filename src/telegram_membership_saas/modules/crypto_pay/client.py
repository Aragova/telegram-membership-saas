"""Strict Crypto Pay REST adapter; authoritative invoice reads, no raw secret logs.

The official API supports getMe, createInvoice and getInvoices on both
mainnet/testnet. It does not identify a Telegram buyer reliably by itself:
payment verification MUST bind the exact app, invoice ID, payload and frozen
quote to the local buyer/order in one transaction before granting anything.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

MAINNET = "https://pay.crypt.bot/api/"
TESTNET = "https://testnet-pay.crypt.bot/api/"
FIAT = frozenset({
    "RUB", "USD", "EUR", "UAH", "BYN", "GBP", "CNY", "KZT",
    "UZS", "GEL", "TRY", "AMD", "THB", "INR", "BRL", "IDR", "AZN", "AED", "PLN",
})
CRYPTO = frozenset({"USDT", "TON", "BTC", "ETH", "LTC", "BNB", "TRX", "USDC"})


class CryptoPayError(RuntimeError):
    """Deliberately token-free diagnostic, safe for user-facing error handling."""


def _safe_decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CryptoPayError("Invalid Crypto Pay amount") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise CryptoPayError("Invalid Crypto Pay amount")
    return parsed


def invoice_price_spec(currency: str, amount: Decimal) -> dict[str, str]:
    """Use seller's frozen currency directly; no invented USD/FX quote."""
    code = currency.upper()
    value = _safe_decimal(amount)
    if code in FIAT:
        if value.as_tuple().exponent < -2 and value != value.quantize(Decimal("0.01")):
            raise ValueError("Crypto Pay fiat prices require at most 2 decimals")
        kind, field = "fiat", "fiat"
    elif code in CRYPTO:
        kind, field = "crypto", "asset"
    else:
        raise ValueError("This price currency is not supported by Crypto Pay")
    return {
        "currency_type": kind,
        field: code,
        "amount": format(value, "f"),
    }


def _invoice_link(link: object, *, testnet: bool) -> str:
    """Crypto Pay returns a Telegram deep link, never trust arbitrary redirects."""
    if not isinstance(link, str):
        raise CryptoPayError("Crypto Pay returned an invalid invoice URL")
    parts = urlsplit(link)
    # @send was the original public @CryptoBot address and still appears
    # in provider-issued single-use invoice deep links.
    allowed = {"cryptotestnetbot"} if testnet else {"cryptobot", "send"}
    if (parts.scheme != "https"
        or parts.username is not None or parts.password is not None
        or parts.port is not None
        or parts.hostname not in {"t.me", "telegram.me"}
        or parts.path.strip("/").lower() not in allowed):
        raise CryptoPayError("Crypto Pay returned an unsafe invoice URL")
    return link


@dataclass(frozen=True, slots=True)
class CryptoPayInvoiceReceipt:
    invoice_id: int
    paid_at: datetime
    payload: str
    amount: Decimal
    currency_code: str
    currency_type: str
    # Crypto Pay's immutable asset valuation at payment (not today's spot).
    # Optional: payments missing a signed provider rate remain unpriced,
    # but still settle and deliver the original purchase.
    paid_usd_rate: Decimal | None = None


def verify_paid_invoice(
    provider_invoice: Mapping[str, Any], *,
    invoice_id: int, payload: str, amount: Decimal,
    currency_code: str, currency_type: str,
) -> CryptoPayInvoiceReceipt:
    """Validate actual provider response, not buyer's button or webhook alone."""
    if provider_invoice.get("status") != "paid":
        raise CryptoPayError("Crypto Pay has not confirmed this payment")
    if (type(provider_invoice.get("invoice_id")) is not int
        or provider_invoice["invoice_id"] != invoice_id
        or provider_invoice.get("payload") != payload
        or provider_invoice.get("currency_type") != currency_type):
        raise CryptoPayError("Crypto Pay invoice identity mismatch")
    field = "fiat" if currency_type == "fiat" else "asset"
    if provider_invoice.get(field) != currency_code:
        raise CryptoPayError("Crypto Pay invoice currency mismatch")
    if _safe_decimal(provider_invoice.get("amount")) != _safe_decimal(amount):
        raise CryptoPayError("Crypto Pay invoice amount mismatch")
    stamp = provider_invoice.get("paid_at")
    if not isinstance(stamp, str):
        raise CryptoPayError("Crypto Pay paid timestamp is missing")
    try:
        paid_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CryptoPayError("Crypto Pay paid timestamp is invalid") from exc
    if paid_at.tzinfo is None or paid_at > datetime.now(UTC) + timedelta(minutes=5):
        raise CryptoPayError("Crypto Pay paid timestamp is invalid")
    # The official getInvoices Invoice.paid_usd_rate is the USD price of
    # the paid asset AT PAYMENT. Never use getExchangeRates(now) to value
    # a historical crypto-denominated sale. Missing rates remain unresolved.
    rate_value = provider_invoice.get("paid_usd_rate")
    paid_usd_rate = (
        _safe_decimal(rate_value) if rate_value not in (None, "") else None
    )
    if paid_usd_rate is not None and paid_usd_rate > Decimal("1000000000"):
        raise CryptoPayError("Crypto Pay paid USD rate is out of bounds")
    return CryptoPayInvoiceReceipt(
        invoice_id=invoice_id, paid_at=paid_at.astimezone(UTC),
        payload=payload, amount=_safe_decimal(amount),
        currency_code=currency_code, currency_type=currency_type,
        paid_usd_rate=paid_usd_rate if currency_type == "crypto" else None,
    )


def verify_webhook_signature(
    raw_body: bytes, signature: str, token: str,
) -> bool:
    """HMAC-SHA256(key=SHA256(app token), message=RAW request bytes)."""
    if not isinstance(signature, str) or len(signature) != 64:
        return False
    key = hashlib.sha256(token.encode("utf-8")).digest()
    expected = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


class CryptoPayClient:
    """One app token per merchant; never log or serialize the token."""

    def __init__(
        self, token: str, *, testnet: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token or not token.isascii() or any(c.isspace() for c in token):
            raise CryptoPayError("Crypto Pay API token is invalid")
        self._token = token
        self.testnet = testnet
        self._client = httpx.AsyncClient(
            base_url=TESTNET if testnet else MAINNET,
            transport=transport, timeout=httpx.Timeout(12.0),
            follow_redirects=False,
        )

    async def __aenter__(self) -> CryptoPayClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, object]) -> object:
        try:
            response = await self._client.post(
                method, json=payload, headers={"Crypto-Pay-API-Token": self._token},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            # Even transport exceptions may contain an authorization header.
            raise CryptoPayError("Crypto Pay API is temporarily unavailable") from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise CryptoPayError("Crypto Pay rejected the request")
        return body.get("result")

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        if not isinstance(result, dict):
            raise CryptoPayError("Crypto Pay app identity is invalid")
        return result

    async def create_invoice(
        self, *, currency: str, amount: Decimal,
        payload: str, description: str, expires_in: int = 1800,
    ) -> dict[str, Any]:
        if not payload or len(payload.encode("utf-8")) > 128:
            raise ValueError("Crypto Pay local invoice payload is invalid")
        if not 60 <= expires_in <= 24 * 3600:
            raise ValueError("Crypto Pay invoice expiry is invalid")
        spec = invoice_price_spec(currency, amount)
        result = await self._call("createInvoice", {
            **spec, "payload": payload, "description": description[:1024],
            "expires_in": expires_in, "allow_comments": False,
            "allow_anonymous": True,
        })
        if not isinstance(result, dict) or type(result.get("invoice_id")) is not int:
            raise CryptoPayError("Crypto Pay invoice creation is incomplete")
        if result.get("payload") != payload or result.get("status") != "active":
            raise CryptoPayError("Crypto Pay returned a mismatched invoice")
        _invoice_link(result.get("bot_invoice_url"), testnet=self.testnet)
        if (result.get("currency_type") != spec["currency_type"]
            or result.get("fiat" if spec["currency_type"] == "fiat" else "asset")
            != currency
            or _safe_decimal(result.get("amount")) != _safe_decimal(amount)):
            raise CryptoPayError("Crypto Pay returned a mismatched invoice")
        return result

    async def get_invoices(self, invoice_ids: tuple[int, ...]) -> tuple[dict, ...]:
        if not invoice_ids or len(invoice_ids) > 100:
            raise ValueError("Crypto Pay invoice lookup requires 1–100 IDs")
        if any(type(n) is not int or n <= 0 for n in invoice_ids):
            raise ValueError("Invalid Crypto Pay invoice ID")
        result = await self._call(
            "getInvoices", {"invoice_ids": ",".join(map(str, invoice_ids))},
        )
        # Current Crypto Pay responses commonly wrap the invoices as
        # result.items (with count). Accept the documented flat array too.
        # Neither shape is sufficient evidence of a paid purchase until
        # verify_paid_invoice checks the exact frozen original order.
        items = result.get("items") if isinstance(result, dict) else result
        if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            raise CryptoPayError("Crypto Pay invoice lookup is incomplete")
        if any(type(x.get("invoice_id")) is not int for x in items):
            raise CryptoPayError("Crypto Pay invoice ID is invalid")
        received = [x["invoice_id"] for x in items]
        if len(set(received)) != len(received):
            raise CryptoPayError("Crypto Pay returned duplicate invoices")
        if not set(received) <= set(invoice_ids):
            raise CryptoPayError("Crypto Pay returned unrelated invoice data")
        return tuple(items)


    async def list_recent_invoices(
        self, *, offset: int = 0, count: int = 100,
    ) -> tuple[dict, ...]:
        """Bounded provider listing for recovering uncertain createInvoice calls.

        Crypto Pay does not offer a documented invoice-payload lookup or
        idempotency key. Never blindly recreate an uncertain request.
        """
        if type(offset) is not int or not 0 <= offset <= 10_000:
            raise ValueError("Invalid Crypto Pay invoice page offset")
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("Invalid Crypto Pay invoice page size")
        result = await self._call("getInvoices", {"offset": offset, "count": count})
        items = result.get("items") if isinstance(result, dict) else result
        if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            raise CryptoPayError("Crypto Pay invoice listing is incomplete")
        ids = [entry.get("invoice_id") for entry in items]
        if any(type(i) is not int or i <= 0 for i in ids):
            raise CryptoPayError("Crypto Pay invoice listing contains invalid IDs")
        if len(set(ids)) != len(ids):
            raise CryptoPayError("Crypto Pay invoice listing contains duplicate IDs")
        return tuple(items)


def verify_created_invoice_snapshot(
    provider_invoice: Mapping[str, Any], *,
    payload: str, amount: Decimal, currency_code: str,
    currency_type: str, testnet: bool,
) -> tuple[int, str | None]:
    """Bind provider evidence to its immutable original local order.

    An expired invoice is terminal: its identity can be recorded without
    returning a payable link. A paid invoice is still NOT an access grant.
    """
    invoice_id = provider_invoice.get("invoice_id")
    status = provider_invoice.get("status")
    if (type(invoice_id) is not int or invoice_id <= 0
        or provider_invoice.get("payload") != payload
        or status not in ("active", "paid", "expired")
        or provider_invoice.get("currency_type") != currency_type):
        raise CryptoPayError("Crypto Pay recovered invoice identity mismatch")
    field = "fiat" if currency_type == "fiat" else "asset"
    if (provider_invoice.get(field) != currency_code
        or _safe_decimal(provider_invoice.get("amount")) != _safe_decimal(amount)):
        raise CryptoPayError("Crypto Pay recovered invoice price mismatch")
    if status == "expired":
        # Do not mistake contradictory paid evidence for a terminal expiration.
        if provider_invoice.get("paid_at") not in (None, ""):
            raise CryptoPayError("Crypto Pay invoice has conflicting paid evidence")
        return invoice_id, None
    link = _invoice_link(provider_invoice.get("bot_invoice_url"), testnet=testnet)
    if status == "paid":
        verify_paid_invoice(
            provider_invoice, invoice_id=invoice_id, payload=payload,
            amount=amount, currency_code=currency_code,
            currency_type=currency_type,
        )
    return invoice_id, link
