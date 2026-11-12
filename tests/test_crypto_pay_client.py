"""Crypto Pay API contract tests: no live HTTP, no real merchant credentials."""
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from telegram_membership_saas.modules.crypto_pay.client import (
    CryptoPayClient,
    CryptoPayError,
    invoice_price_spec,
    verify_completed_transfer,
    verify_created_invoice_snapshot,
    verify_paid_invoice,
    verify_webhook_signature,
)

TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.mark.parametrize(("code", "kind", "field"), [
    ("RUB", "fiat", "fiat"),
    ("USD", "fiat", "fiat"),
    ("EUR", "fiat", "fiat"),
    ("UAH", "fiat", "fiat"),
    ("BYN", "fiat", "fiat"),
    ("USDT", "crypto", "asset"),
    ("TON", "crypto", "asset"),
])
def test_prices_preserve_original_seller_currency(code, kind, field):
    result = invoice_price_spec(code, Decimal("500.50"))
    assert result["currency_type"] == kind
    assert result[field] == code
    assert result["amount"] == "500.50"


def test_never_guess_currency_or_accept_floating_point_price():
    for code in ("XTR", "JPY", "FAKE"):
        with pytest.raises(ValueError):
            invoice_price_spec(code, Decimal("100"))
    with pytest.raises(ValueError):
        invoice_price_spec("RUB", Decimal("1.234"))
    with pytest.raises(CryptoPayError):
        invoice_price_spec("RUB", Decimal("NaN"))
    with pytest.raises(CryptoPayError):
        invoice_price_spec("USD", Decimal("-4"))


async def test_auth_create_and_get_invoices_use_only_official_host_and_header():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "testnet-pay.crypt.bot"
        assert request.headers["Crypto-Pay-API-Token"] == TOKEN
        assert request.url.scheme == "https"
        calls.append(request.url.path)
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"name": "Seller"}})
        if request.url.path.endswith("/createInvoice"):
            payload = __import__("json").loads(request.content)
            assert payload["currency_type"] == "fiat"
            assert payload["fiat"] == "RUB"
            assert payload["amount"] == "550"
            assert payload["payload"] == "cp:test123"
            return httpx.Response(200, json={"ok": True, "result": {
                "invoice_id": 7782, "payload": "cp:test123",
                "currency_type": "fiat", "fiat": "RUB", "amount": "550",
                "status": "active",
                "bot_invoice_url": "https://t.me/CryptoTestnetBot?start=IV7782",
            }})
        if request.url.path.endswith("/getInvoices"):
            return httpx.Response(200, json={"ok": True, "result": {"items": [{
                "invoice_id": 7782, "status": "paid", "payload": "cp:test123",
                "currency_type": "fiat", "fiat": "RUB", "amount": "550",
                "paid_at": datetime.now(UTC).isoformat(),
            }], "count": 1}})
        raise AssertionError("unexpected API method")

    async with CryptoPayClient(
        TOKEN, testnet=True, transport=httpx.MockTransport(handler),
    ) as api:
        assert (await api.get_me())["name"] == "Seller"
        invoice = await api.create_invoice(
            currency="RUB", amount=Decimal("550"), payload="cp:test123",
            description="Monthly premium",
        )
        assert invoice["invoice_id"] == 7782
        receipts = await api.get_invoices((7782,))
        paid = verify_paid_invoice(
            receipts[0], invoice_id=7782,
            payload="cp:test123", amount=Decimal("550.00"),
            currency_code="RUB", currency_type="fiat",
        )
        assert paid.invoice_id == 7782
    assert calls == ["/api/getMe", "/api/createInvoice", "/api/getInvoices"]


def _paid() -> dict:
    return {
        "status": "paid", "invoice_id": 42, "payload": "cp:unique",
        "currency_type": "fiat", "fiat": "EUR", "amount": "19.99",
        "paid_at": (datetime.now(UTC) - timedelta(minutes=3)).isoformat(),
    }


@pytest.mark.parametrize(("field", "value"), [
    ("status", "active"),
    ("invoice_id", 41),
    ("payload", "cp:someone-else"),
    ("fiat", "USD"),
    ("amount", "19.98"),
    ("currency_type", "crypto"),
    ("paid_at", "9999-01-01T00:00:00+00:00"),
    ("paid_at", "2026-11-12T10:00:00"),
])
def test_paid_receipt_must_match_frozen_invoice_exactly(field, value):
    data = _paid()
    data[field] = value
    with pytest.raises(CryptoPayError):
        verify_paid_invoice(
            data, invoice_id=42, payload="cp:unique",
            amount=Decimal("19.99"), currency_code="EUR", currency_type="fiat",
        )


def test_webhook_signature_uses_exact_raw_body_and_constant_time_digest():
    body = b'{"update_type":"invoice_paid","payload":{"invoice_id":42}}'
    secret = hashlib.sha256(TOKEN.encode()).digest()
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, sig, TOKEN)
    assert not verify_webhook_signature(body + b" ", sig, TOKEN)
    assert not verify_webhook_signature(body, "not-a-real-signature", TOKEN)
    assert not verify_webhook_signature(body, sig, TOKEN + "wrong")


async def test_provider_errors_never_echo_api_token():
    def fail(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": TOKEN})

    async with CryptoPayClient(
        TOKEN, transport=httpx.MockTransport(fail),
    ) as api:
        with pytest.raises(CryptoPayError) as exc:
            await api.get_me()
        assert TOKEN not in str(exc.value)


async def test_provider_must_return_an_official_crypto_bot_link():
    def spoof(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {
            "invoice_id": 42, "payload": "cp:abc",
            "currency_type": "crypto", "asset": "TON", "amount": "2",
            "status": "active", "bot_invoice_url": "https://evil.example/pay",
        }})
    async with CryptoPayClient(
        TOKEN, transport=httpx.MockTransport(spoof),
    ) as api:
        with pytest.raises(CryptoPayError, match="unsafe"):
            await api.create_invoice(
                currency="TON", amount=Decimal("2"),
                payload="cp:abc", description="Premium",
            )


@pytest.mark.parametrize("payload,valid", [
    ({"items": [{"invoice_id": 3}], "count": 1}, True),
    ([{"invoice_id": 3}], True),  # Documented legacy flat-array response.
    ({"items": [{"invoice_id": 4}]}, False),
    ({"items": [{"invoice_id": 3}, {"invoice_id": 3}]}, False),
    ({"items": "bad"}, False),
    ({"items": [{"invoice_id": "3"}]}, False),
])
async def test_get_invoices_accepts_provider_envelope_but_rejects_spoofing(
    payload, valid,
):
    def response(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": payload})

    async with CryptoPayClient(
        TOKEN, transport=httpx.MockTransport(response),
    ) as api:
        if valid:
            invoices = await api.get_invoices((3,))
            assert len(invoices) == 1 and invoices[0]["invoice_id"] == 3
        else:
            with pytest.raises(CryptoPayError):
                await api.get_invoices((3,))


async def test_official_legacy_cryptobot_invoice_link_is_allowed():
    def response(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {
            "invoice_id": 35, "payload": "cp:oldlink",
            "currency_type": "fiat", "fiat": "RUB", "amount": "120",
            "status": "active",
            "bot_invoice_url": "https://t.me/send?start=IV35",
        }})

    async with CryptoPayClient(TOKEN, transport=httpx.MockTransport(response)) as api:
        invoice = await api.create_invoice(
            currency="RUB", amount=Decimal("120"),
            payload="cp:oldlink", description="Payment",
        )
        assert invoice["invoice_id"] == 35


async def test_recent_invoice_listing_is_bounded_and_requires_valid_provider_ids():
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        args = json.loads(request.content)
        assert args == {"offset": 100, "count": 50}
        return httpx.Response(200, json={
            "ok": True, "result": {"items": [
                {"invoice_id": 42, "payload": "cp:original"},
            ], "count": 1},
        })

    async with CryptoPayClient(
        TOKEN, testnet=True, transport=httpx.MockTransport(handler),
    ) as api:
        assert (await api.list_recent_invoices(offset=100, count=50))[0][
            "invoice_id"
        ] == 42
        with pytest.raises(ValueError):
            await api.list_recent_invoices(offset=-1, count=50)
        with pytest.raises(ValueError):
            await api.list_recent_invoices(count=1001)


def test_uncertain_invoice_recovery_checks_price_payload_and_official_url():
    template = {
        "invoice_id": 42,
        "status": "active", "payload": "cp:unique",
        "currency_type": "fiat", "fiat": "RUB", "amount": "100",
        "bot_invoice_url": "https://t.me/CryptoTestnetBot?start=IV42",
    }
    kwargs = dict(
        payload="cp:unique", amount=Decimal("100.00"),
        currency_code="RUB", currency_type="fiat", testnet=True,
    )
    assert verify_created_invoice_snapshot(template, **kwargs)[0] == 42
    for field, replacement in (
        ("payload", "cp:other"),
        ("amount", "101"),
        ("fiat", "EUR"),
        ("bot_invoice_url", "https://invalid.example/pay"),
    ):
        invalid = {**template, field: replacement}
        with pytest.raises(CryptoPayError):
            verify_created_invoice_snapshot(invalid, **kwargs)



def test_recovered_expired_invoice_is_terminal_without_a_payment_link():
    snapshot = {
        "invoice_id": 93, "payload": "cp:expired", "status": "expired",
        "currency_type": "fiat", "fiat": "EUR", "amount": "15.00",
    }
    expected = dict(
        payload="cp:expired", amount=Decimal("15.00"),
        currency_code="EUR", currency_type="fiat", testnet=False,
    )
    # Provider may omit bot_invoice_url on expired invoices.
    # Never surface an old or malformed payment URL after expiry.
    assert verify_created_invoice_snapshot(snapshot, **expected) == (93, None)
    for change in (
        {"amount": "15.01"}, {"fiat": "USD"},
        {"payload": "cp:someone-else"},
        {"status": "cancelled"}, {"paid_at": datetime.now(UTC).isoformat()},
    ):
        altered = {**snapshot, **change}
        with pytest.raises(CryptoPayError):
            verify_created_invoice_snapshot(altered, **expected)


async def test_verified_crypto_refund_transfer_requires_original_spend_and_buyer():
    spend_id = "vimi-refund:11111111-1111-4111-8111-111111111111:abcd1234"
    paid_at = datetime.now(UTC).isoformat()
    response = {
        "transfer_id": 1703, "spend_id": spend_id,
        "status": "completed", "user_id": "741852",
        "asset": "TON", "amount": "0.25", "completed_at": paid_at,
    }
    result = verify_completed_transfer(
        response, spend_id=spend_id, buyer_id=741852, asset_code="TON",
    )
    assert result.transfer_id == 1703
    assert result.amount == Decimal("0.25")
    for mutated in (
        {"spend_id": "other"}, {"user_id": "123"}, {"asset": "BTC"},
        {"status": "pending"}, {"transfer_id": -1},
        {"completed_at": "2026-11-12T10:00:00"},
        {"amount": "0.0000000000000000001"},
    ):
        with pytest.raises(CryptoPayError):
            verify_completed_transfer(
                {**response, **mutated}, spend_id=spend_id,
                buyer_id=741852, asset_code="TON",
            )


async def test_readonly_crypto_transfer_query_is_bounded_and_safe():
    spend_id = "vimi-refund:11111111-1111-4111-8111-111111111111:abcd1234"

    def fake(request: httpx.Request) -> httpx.Response:
        import json

        assert request.url.host == "testnet-pay.crypt.bot"
        assert request.url.path.endswith("/getTransfers")
        assert json.loads(request.content) == {"spend_id": spend_id}
        return httpx.Response(200, json={"ok": True, "result": {"items": [{
            "transfer_id": 1703, "spend_id": spend_id,
            "status": "completed", "user_id": "741852",
            "asset": "TON", "amount": "0.25",
            "completed_at": datetime.now(UTC).isoformat(),
        }]}})

    async with CryptoPayClient(
        TOKEN, testnet=True, transport=httpx.MockTransport(fake),
    ) as provider:
        rows = await provider.get_transfers(spend_id=spend_id)
        assert len(rows) == 1
        with pytest.raises(ValueError):
            await provider.get_transfers(spend_id="")

