"""Strict platform-owned lava.top v3 dynamic-price invoice adapter.

Lava API key stays in the owner-controlled central runtime, never in
connected tenant bots, callbacks, logs or this repository. This is provider
I/O only: a paid state from Lava is NOT a platform ledger allocation.
The independent original-invoice SQL allocation must verify the returned
frozen amount/ID and idempotency before extending any bot service.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

LAVA_API_BASE = "https://gate.lava.top"
LAVA_LIMITS: dict[str, tuple[Decimal, Decimal]] = {
    "RUB": (Decimal("50"), Decimal("1000000")),
    "USD": (Decimal("5"), Decimal("10000")),
    "EUR": (Decimal("5"), Decimal("10000")),
}


class LavaTopError(RuntimeError):
    """Safe, credential-free provider error."""


def _amount(value: Decimal | str | int, currency: str) -> Decimal:
    if currency not in LAVA_LIMITS:
        raise ValueError("Unsupported Lava.top currency")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid Lava.top amount") from exc
    low, high = LAVA_LIMITS[currency]
    if (
        not parsed.is_finite() or parsed < low or parsed > high
        or parsed != parsed.quantize(Decimal("0.01"))
    ):
        raise ValueError("Lava.top amount outside permitted price limits")
    return parsed


def _provider_invoice_id(value: object) -> UUID:
    if not isinstance(value, str):
        raise LavaTopError("Invalid Lava.top invoice identity")
    try:
        return UUID(value)
    except ValueError as exc:
        raise LavaTopError("Invalid Lava.top invoice identity") from exc


def _checkout_link(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise LavaTopError("Lava.top returned an invalid checkout link")
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https" or not hostname
        or not (hostname == "lava.top" or hostname.endswith(".lava.top"))
        or parsed.username is not None or parsed.password is not None
        or parsed.port is not None or parsed.fragment
    ):
        raise LavaTopError("Lava.top returned an unsafe checkout link")
    return value


@dataclass(frozen=True, slots=True)
class LavaTopCreatedInvoice:
    invoice_id: UUID
    payment_url: str
    currency: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class LavaTopPaidInvoice:
    invoice_id: UUID
    currency: str
    amount: Decimal


def _unwrap(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LavaTopError("Lava.top returned an invalid response")
    # Some endpoints expose the envelope "data"; never accept conflicting
    # top-level and nested identities or treat a boolean as payment proof.
    if "data" in raw:
        value = raw["data"]
        if not isinstance(value, dict):
            raise LavaTopError("Lava.top returned an invalid response body")
        if any(k in raw for k in ("id", "contractId", "invoiceId", "status")):
            raise LavaTopError("Lava.top returned conflicting invoice data")
        return value
    return raw


def _id_from(raw: Mapping[str, Any]) -> UUID:
    ids = [raw[k] for k in ("id", "invoiceId", "contractId") if k in raw]
    if not ids:
        raise LavaTopError("Lava.top did not return the invoice ID")
    resolved = [_provider_invoice_id(x) for x in ids]
    if len(set(resolved)) != 1:
        raise LavaTopError("Lava.top returned conflicting invoice IDs")
    return resolved[0]


def verify_lava_paid_invoice(
    provider_record: Mapping[str, Any], *,
    expected_id: UUID, expected_currency: str, expected_amount: Decimal,
) -> LavaTopPaidInvoice:
    """A provider-authenticated GET, never a redirect or unverified webhook.

    The caller must additionally check the frozen local invoice/order,
    current owner, refund evidence and exact-once platform SQL allocation.
    """
    data = _unwrap(dict(provider_record))
    found_id = _id_from(data)
    if found_id != expected_id:
        raise LavaTopError("Lava.top invoice ID mismatch")
    if data.get("status") != "completed" or data.get("type") != "ONE_TIME":
        raise LavaTopError("Lava.top did not confirm completed one-time payment")
    receipt = data.get("receipt")
    if not isinstance(receipt, dict):
        raise LavaTopError("Lava.top invoice receipt is missing")
    if receipt.get("currency") != expected_currency:
        raise LavaTopError("Lava.top invoice currency mismatch")
    if _amount(expected_amount, expected_currency) != _amount(
        receipt.get("amount"), expected_currency,
    ):
        raise LavaTopError("Lava.top invoice amount mismatch")
    return LavaTopPaidInvoice(
        invoice_id=found_id, currency=expected_currency,
        amount=expected_amount,
    )


class LavaTopClient:
    def __init__(
        self, api_key: str, *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            not isinstance(api_key, str) or not api_key
            or not api_key.isascii() or any(c.isspace() for c in api_key)
        ):
            raise ValueError("Invalid lava.top API key")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=LAVA_API_BASE,
            transport=transport,
            timeout=httpx.Timeout(12.0),
            follow_redirects=False,
        )

    async def __aenter__(self) -> LavaTopClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, json: dict | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, path, json=json,
                headers={
                    "Accept": "application/json",
                    "X-Api-Key": self._api_key,
                },
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError):
            # HTTP exceptions can contain the request's secret header.
            raise LavaTopError("Lava.top API is temporarily unavailable") from None
        return _unwrap(result)

    async def create_dynamic_invoice(
        self, *, email: str, offer_id: UUID,
        amount: Decimal, currency: str,
    ) -> LavaTopCreatedInvoice:
        total = _amount(amount, currency)
        if (
            not isinstance(email, str) or not 5 <= len(email) <= 254
            or email != email.strip() or email.count("@") != 1
            or any(c.isspace() for c in email)
        ):
            raise ValueError("A verified payer email is required for Lava.top")
        data = await self._request("POST", "/api/v3/invoice", json={
            "email": email,
            "offerId": str(offer_id),
            "currency": currency,
            "amount": float(total),
        })
        if data.get("status") not in ("new", "in-progress"):
            raise LavaTopError("Lava.top invoice is not payable")
        amount_total = data.get("amountTotal")
        if not isinstance(amount_total, dict):
            raise LavaTopError("Lava.top invoice amount snapshot missing")
        if (
            amount_total.get("currency") != currency
            or _amount(amount_total.get("amount"), currency) != total
        ):
            raise LavaTopError("Lava.top invoice quote differs from original")
        provider_id = _id_from(data)
        url = _checkout_link(data.get("paymentUrl"))
        return LavaTopCreatedInvoice(
            invoice_id=provider_id, payment_url=url,
            currency=currency, amount=total,
        )

    async def get_invoice(
        self, *, invoice_id: UUID,
    ) -> dict[str, Any]:
        return await self._request(
            "GET", f"/api/v1/invoices/{invoice_id}",
        )
