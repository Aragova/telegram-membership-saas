"""Fail-closed offline tests for ViMi Pay's owner-owned lava.top adapter."""
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from telegram_membership_saas.modules.platform_saas_lava_client import (
    LavaTopClient,
    LavaTopError,
    _checkout_link,
    _amount,
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
            "data": {"invoiceId": str(invoice),
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
    {"invoiceId": "not-uuid", "paymentUrl": "https://lava.top/pay"},
    {"invoiceId": str(uuid4()), "paymentUrl": "https://evil.example/pay"},
    {"invoiceId": str(uuid4()), "paymentUrl": "http://lava.top/pay"},
    {"invoiceId": str(uuid4())},
    {"invoiceId": str(uuid4()), "contractId": str(uuid4()),
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
    {"status": "pending"},
    {"status": "failed"},
    {"status": "SUCCESS", "currency": "USD"},
    {"status": "SUCCESS", "amount": 149},
    {"status": "SUCCESS", "amount": "NaN"},
])
def test_verified_settlement_requires_exact_provider_identity(reply):
    invoice_id = uuid4()
    frozen = {
        "invoiceId": str(invoice_id),
        "status": "SUCCESS",
        "currency": "RUB",
        "amount": 150,
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
        "contractId": str(invoice_id), "status": "SUCCESS",
        "currency": "RUB", "amount": "150.00",
    }, expected_id=invoice_id, expected_currency="RUB",
        expected_amount=Decimal("150.00"))
    assert result.invoice_id == invoice_id


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
