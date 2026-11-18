"""Fail-closed offline tests for ViMi Pay's owner-owned lava.top adapter."""
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from telegram_membership_saas.modules.platform_saas_lava_client import (
    LavaTopClient,
    LavaTopError,
    _amount,
    _checkout_link,
    verify_lava_paid_invoice,
)


@pytest.mark.parametrize(("currency", "min_amount", "max_amount"), [
    ("RUB", "50", "1000000"),
    ("USD", "5", "10000"),
    ("EUR", "5", "10000"),
])
def test_dynamic_price_strict_provider_bounds(currency, min_amount, max_amount):
    assert _amount(Decimal(min_amount), currency) == Decimal(min_amount)
    assert _amount(Decimal(max_amount), currency) == Decimal(max_amount)
    with pytest.raises(ValueError):
        _amount(Decimal(min_amount)-Decimal("0.01"), currency)
    with pytest.raises(ValueError):
        _amount(Decimal(max_amount)+Decimal("0.01"), currency)


@pytest.mark.parametrize("value", [
    "NaN", "Infinity", "150.001", "-1", "0", "0.00",
])
def test_invalid_amount_rejected(value):
    with pytest.raises(ValueError):
        _amount(Decimal(value), "RUB")


@pytest.mark.parametrize("link", [
    "http://lava.top/pay/abc",
    "https://lava.top.evil.example/pay",
    "https://lava.top@evil.example/pay",
    "https://evil.example/pay",
    "https://lava.top:444/pay",
    "https://user:pass@lava.top/pay",
    "https://lava.top/pay#extra",
])
def test_unsafe_provider_redirect_rejected(link):
    with pytest.raises(LavaTopError):
        _checkout_link(link)


def test_provider_issued_safe_checkout_link():
    assert _checkout_link("https://app.lava.top/pay/abc?order=123").startswith("https:")


async def test_create_invoice_uses_v3_dynamic_price_and_secret_header():
    invoice = uuid4()
    offer = uuid4()
    observed = []

    def handler(request: httpx.Request):
        observed.append(request)
        assert request.url.host == "gate.lava.top"
        assert request.url.path == "/api/v3/invoice"
        assert request.method == "POST"
        assert request.headers["X-Api-Key"] == "TEST_SECRET_KEEP_PRIVATE"
        import json

        data = json.loads(request.content)
        assert data == {
            "email": "merchant@example.com",
            "offerId": str(offer),
            "currency": "RUB",
            "amount": 150.0,
        }
        return httpx.Response(200, json={
            "data": {"id": str(invoice), "status": "new",
                     "amountTotal": {"amount": 150, "currency": "RUB"},
                     "paymentUrl": "https://app.lava.top/checkout/abc"},
        })

    async with LavaTopClient(
        "TEST_SECRET_KEEP_PRIVATE", transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.create_dynamic_invoice(
            email="merchant@example.com", offer_id=offer,
            amount=Decimal("150.00"), currency="RUB",
        )
    assert result.invoice_id == invoice
    assert result.currency == "RUB"
    assert result.amount == Decimal("150.00")
    assert len(observed) == 1


@pytest.mark.parametrize("result", [
    {"id": "not-uuid", "status": "new", "amountTotal":
     {"amount": 150, "currency": "RUB"}, "paymentUrl": "https://lava.top/pay"},
    {"id": str(uuid4()), "status": "new", "amountTotal":
     {"amount": 150, "currency": "RUB"}, "paymentUrl": "https://evil.example/pay"},
    {"id": str(uuid4()), "status": "new", "amountTotal":
     {"amount": 150, "currency": "RUB"}, "paymentUrl": "http://lava.top/pay"},
    {"id": str(uuid4()), "status": "new", "amountTotal":
     {"amount": 150, "currency": "RUB"}},
    {"id": str(uuid4()), "invoiceId": str(uuid4()),
     "status": "new", "amountTotal": {"amount": 150, "currency": "RUB"},
     "paymentUrl": "https://lava.top/pay"},
])
async def test_create_fails_closed_on_untrusted_provider_reply(result):
    async with LavaTopClient(
        "test", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=result),
        ),
    ) as client:
        with pytest.raises(LavaTopError):
            await client.create_dynamic_invoice(
                email="buyer@example.com", offer_id=uuid4(),
                currency="RUB", amount=Decimal("150"),
            )


@pytest.mark.parametrize("reply", [
    {"status": "new"},
    {"status": "failed"},
    {"receipt": {"amount": 150, "currency": "USD", "fee": 0}},
    {"receipt": {"amount": 149, "currency": "RUB", "fee": 0}},
    {"receipt": {"amount": "NaN", "currency": "RUB", "fee": 0}},
    {"type": "RECURRING"},
])
def test_verified_settlement_requires_exact_provider_identity(reply):
    invoice_id = uuid4()
    frozen = {
        "invoiceId": str(invoice_id),
        "status": "completed", "type": "ONE_TIME",
        "receipt": {"currency": "RUB", "amount": 150, "fee": 0},
    }
    frozen.update(reply)
    with pytest.raises((LavaTopError, ValueError)):
        verify_lava_paid_invoice(
            frozen, expected_id=invoice_id,
            expected_currency="RUB", expected_amount=Decimal("150.00"),
        )


def test_verified_settlement_accepts_matching_provider_get():
    invoice_id = uuid4()
    result = verify_lava_paid_invoice({
        "id": str(invoice_id), "status": "completed",
        "type": "ONE_TIME",
        "receipt": {"currency": "RUB", "amount": "150.00", "fee": 0},
    }, expected_id=invoice_id, expected_currency="RUB",
        expected_amount=Decimal("150.00"))
    assert result.invoice_id == invoice_id


async def test_authoritative_lava_get_uses_sdk_v2_receipt_snapshot():
    """Provider GET /api/v1/invoices/{id} uses nested receipt, not flat amount."""
    invoice_id = uuid4()

    def provider(request: httpx.Request):
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/invoices/{invoice_id}"
        return httpx.Response(200, json={
            # These are the Lava SDK InvoiceResponseV2 fields.
            "id": str(invoice_id),
            "type": "ONE_TIME",
            "datetime": "2026-11-17T18:00:00Z",
            "status": "completed",
            "receipt": {"amount": 150.0, "currency": "RUB", "fee": 1.5},
            "buyer": {"email": "payer@example.com"},
            "product": {"name": "ViMi Pay service", "offer": "offer-id"},
        })
    async with LavaTopClient(
        "TEST_ONLY_API_KEY", transport=httpx.MockTransport(provider),
    ) as client:
        received = await client.get_invoice(invoice_id=invoice_id)
    paid = verify_lava_paid_invoice(
        received, expected_id=invoice_id,
        expected_currency="RUB", expected_amount=Decimal("150.00"),
    )
    assert paid.invoice_id == invoice_id
    assert paid.amount == Decimal("150.00")


async def test_provider_transport_failure_never_exposes_key():
    def handler(_: httpx.Request):
        raise httpx.ConnectError("unavailable")
    async with LavaTopClient(
        "SECRET_123456", transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(LavaTopError) as exc:
            await client.get_invoice(invoice_id=uuid4())
    assert "SECRET_123456" not in str(exc.value)


async def test_missing_email_never_reaches_provider():
    calls = []
    async with LavaTopClient(
        "key", transport=httpx.MockTransport(
            lambda req: calls.append(req) or httpx.Response(200, json={}),
        ),
    ) as client:
        with pytest.raises(ValueError):
            await client.create_dynamic_invoice(
                email="not-an-email", offer_id=uuid4(),
                amount=Decimal("150"), currency="RUB",
            )
    assert not calls


@pytest.mark.parametrize(("status", "details", "expected"), [
    (400, {"currency": "PRIVATE_EMAIL SECRET_API_KEY"}, "LAVA-HTTP-400-CURRENCY"),
    (400, {"offerId": "PRIVATE_OFFER"}, "LAVA-HTTP-400-OFFER"),
    (422, {"amount": "PRIVATE_AMOUNT"}, "LAVA-HTTP-422-AMOUNT"),
    (401, {"email": "PRIVATE_EMAIL"}, "LAVA-HTTP-401"),
    (403, None, "LAVA-HTTP-403"),
    (404, None, "LAVA-HTTP-404"),
    (429, None, "LAVA-HTTP-429"),
    (503, None, "LAVA-HTTP-503"),
])
async def test_lava_http_rejections_show_safe_diagnostic_only(
    status, details, expected,
):
    response = {
        "error": "SECRET_API_KEY PRIVATE_EMAIL",
        "details": details,
    }
    async with LavaTopClient(
        "SECRET_API_KEY",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json=response),
        ),
    ) as adapter:
        with pytest.raises(LavaTopError) as exc:
            await adapter.create_dynamic_invoice(
                email="private@example.com",
                offer_id=uuid4(), amount=Decimal("150"), currency="RUB",
            )
    assert exc.value.code == expected
    assert "PRIVATE_EMAIL" not in str(exc.value)
    assert "SECRET_API_KEY" not in str(exc.value)
    assert "private@example.com" not in str(exc.value)
    assert "PRIVATE" not in exc.value.code


async def test_arbitrary_provider_error_field_is_not_exposed():
    async with LavaTopClient(
        "TOP_SECRET",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={
                "details": {"TOP_SECRET": "private@example.com"},
                "error": "request echoed TOP_SECRET",
            }),
        ),
    ) as adapter:
        with pytest.raises(LavaTopError) as exc:
            await adapter.create_dynamic_invoice(
                email="private@example.com",
                offer_id=uuid4(), amount=Decimal("150"), currency="RUB",
            )
    assert exc.value.code == "LAVA-HTTP-400"
    assert "TOP_SECRET" not in str(exc.value)


async def test_provider_non_json_2xx_is_distinct_from_http_error():
    async with LavaTopClient(
        "key", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="<html>Not JSON</html>"),
        ),
    ) as adapter:
        with pytest.raises(LavaTopError) as exc:
            await adapter.create_dynamic_invoice(
                email="buyer@example.com", offer_id=uuid4(),
                currency="RUB", amount=Decimal("150"),
            )
    assert exc.value.code == "LAVA-INVALID-JSON"


async def test_unknown_create_response_status_is_diagnosed_without_accepting_link():
    async with LavaTopClient(
        "key", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={
                "id": str(uuid4()), "status": "failed",
                "amountTotal": {"amount": 150, "currency": "RUB"},
                "paymentUrl": "https://lava.top/pay/test",
            }),
        ),
    ) as adapter:
        with pytest.raises(LavaTopError) as exc:
            await adapter.create_dynamic_invoice(
                email="buyer@example.com", offer_id=uuid4(),
                currency="RUB", amount=Decimal("150"),
            )
    assert exc.value.code == "LAVA-CREATE-STATUS"
