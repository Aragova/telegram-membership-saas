"""Pure MockTransport checks for platform-owned Crypto Pay provider adapter."""
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.crypto_pay.client import CryptoPayError
from telegram_membership_saas.modules.platform_saas_crypto_client import (
    _platform_client,
    create_platform_crypto_invoice,
    lookup_platform_crypto_payment,
)


def settings(**kwargs):
    return Settings(
        _env_file=None,
        environment="test",
        telegram_central_access_enabled=False,
        saas_crypto_pay_api_token="FAKE_PLATFORM_APP_TOKEN",
        **kwargs,
    )


async def test_platform_crypto_requires_own_app_not_connected_tenant_token():
    cfg = Settings(
        _env_file=None,
        telegram_central_access_enabled=False,
        saas_crypto_pay_api_token=None,
    )
    with pytest.raises(CryptoPayError):
        _platform_client(cfg)
    cfg = Settings(
        _env_file=None,
        telegram_central_access_enabled=True,
        saas_crypto_pay_api_token="FAKE_PLATFORM_APP_TOKEN",
    )
    with pytest.raises(PermissionError):
        _platform_client(cfg)


def test_platform_crypto_cannot_use_testnet_in_production():
    cfg = Settings(
        _env_file=None, environment="production",
        telegram_central_access_enabled=False,
        saas_crypto_pay_api_token="FAKE_PLATFORM_APP_TOKEN",
        saas_crypto_pay_testnet=True,
    )
    with pytest.raises(PermissionError):
        _platform_client(cfg)


async def test_platform_crypto_issues_frozen_rub_invoice_on_own_app():
    import json

    local = uuid4()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "pay.crypt.bot"
        assert request.url.path == "/api/createInvoice"
        assert request.headers["Crypto-Pay-API-Token"] == "FAKE_PLATFORM_APP_TOKEN"
        args = json.loads(request.content)
        assert args["currency_type"] == "fiat"
        assert args["fiat"] == "RUB"
        assert args["amount"] == "150.00"
        assert args["payload"] == "vpc:" + str(local)
        return httpx.Response(200, json={"ok": True, "result": {
            "invoice_id": 501, "status": "active",
            "payload": args["payload"], "currency_type": "fiat",
            "fiat": "RUB", "amount": "150.00",
            "bot_invoice_url": "https://t.me/CryptoBot?start=IV501",
        }})
    result = await create_platform_crypto_invoice(
        settings(), order_id=local, rub_amount=Decimal("150.00"),
        transport=httpx.MockTransport(handler),
    )
    assert result.provider_invoice_id == 501
    assert result.rub_amount == Decimal("150.00")
    assert result.original_payload == "vpc:" + str(local)
    assert result.bot_invoice_url.startswith("https://t.me/CryptoBot")
    assert len(requests) == 1


@pytest.mark.parametrize("status", ["active", "expired"])
async def test_platform_crypto_pending_provider_does_not_confirm(status):
    payload = "vpc:" + str(uuid4())
    def handler(request: httpx.Request):
        assert request.url.path == "/api/getInvoices"
        return httpx.Response(200, json={"ok": True, "result": {"items": [
            {
                "invoice_id": 501, "status": status,
                "payload": payload, "currency_type": "fiat",
                "fiat": "RUB", "amount": "150.00",
            },
        ]}})
    assert await lookup_platform_crypto_payment(
        settings(), provider_invoice_id=501,
        original_payload=payload, rub_amount=Decimal("150"),
        transport=httpx.MockTransport(handler),
    ) is None


async def test_platform_crypto_exact_provider_paid_proof():
    payload = "vpc:" + str(uuid4())
    paid = datetime.now(UTC).isoformat()
    def handler(_: httpx.Request):
        return httpx.Response(200, json={"ok": True, "result": {"items": [
            {
                "invoice_id": 501, "status": "paid",
                "payload": payload, "currency_type": "fiat",
                "fiat": "RUB", "amount": "150.00",
                "paid_at": paid,
            },
        ]}})
    found = await lookup_platform_crypto_payment(
        settings(), provider_invoice_id=501,
        original_payload=payload, rub_amount=Decimal("150"),
        transport=httpx.MockTransport(handler),
    )
    assert found is not None
    assert found.invoice_id == 501
    assert found.amount == Decimal("150")


@pytest.mark.parametrize("mutation", [
    {"invoice_id": 502},
    {"payload": "vpc:" + str(uuid4())},
    {"amount": "149.99"},
    {"fiat": "USD"},
    {"currency_type": "crypto"},
    {"status": "test"},
])
async def test_platform_crypto_no_credit_for_conflicting_provider_data(mutation):
    payload = "vpc:" + str(uuid4())
    row = {
        "invoice_id": 501, "status": "paid",
        "payload": payload, "currency_type": "fiat",
        "fiat": "RUB", "amount": "150.00",
        "paid_at": datetime.now(UTC).isoformat(),
    }
    row.update(mutation)
    def handler(_: httpx.Request):
        return httpx.Response(200, json={"ok": True, "result": {"items": [row]}})
    with pytest.raises(CryptoPayError):
        await lookup_platform_crypto_payment(
            settings(), provider_invoice_id=501,
            original_payload=payload, rub_amount=Decimal("150"),
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.parametrize("amount", [
    Decimal("0"), Decimal("-1"), Decimal("1.001"), Decimal("NaN"),
])
async def test_platform_crypto_bad_quote_never_requests_provider(amount):
    calls = []
    with pytest.raises(ValueError):
        await create_platform_crypto_invoice(
            settings(), order_id=uuid4(), rub_amount=amount,
            transport=httpx.MockTransport(
                lambda req: calls.append(req) or httpx.Response(200, json={}),
            ),
        )
    assert not calls
