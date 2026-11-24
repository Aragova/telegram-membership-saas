"""Offline coverage of external ViMi Pay gateway provider failure paths.

These tests use mocked HTTP clients and database RPCs. No real invoice is
issued, and no platform settlement/credit is ever written.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from telegram_membership_saas.modules import platform_saas_gateway_service as gateway


def _settings(**changes):
    result = {
        "telegram_central_access_enabled": False,
        "platform_owner_telegram_id": 901,
        "saas_crypto_pay_api_token": "test-only-crypto-token",
        "saas_crypto_pay_testnet": False,
        "saas_lava_top_api_key": SimpleNamespace(
            get_secret_value=lambda: "test-only-lava-token",
        ),
        "saas_lava_top_offer_id": "test-offer",
    }
    result.update(changes)
    return SimpleNamespace(**result)


def _reservation(**changes):
    value = {
        "id": str(uuid4()),
        "status": "reserved",
        "amount_rub": "150.00",
        "pay_currency": "USD",
        "pay_amount": "5.00",
    }
    value.update(changes)
    return value


def _pending(provider="lava", **changes):
    result = {
        "id": str(uuid4()),
        "provider": provider,
        "provider_invoice_id": str(uuid4()),
        "invoice_payload": "vimi-original-order",
        "amount_rub": "150.00",
        "pay_currency": "USD",
        "pay_amount": "5.00",
    }
    result.update(changes)
    return result


class FakeLavaClient:
    def __init__(self, *, created=None, snapshot=None, error=None):
        self.create_dynamic_invoice = AsyncMock(
            return_value=created, side_effect=error,
        )
        self.get_invoice = AsyncMock(return_value=snapshot)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


async def test_crypto_checkout_requires_actual_platform_credentials(monkeypatch):
    reserve = AsyncMock()
    monkeypatch.setattr(gateway, "_reserve", reserve)
    with pytest.raises(gateway.CryptoPayError, match="credentials"):
        await gateway.issue_platform_crypto_checkout(
            object(), settings=_settings(saas_crypto_pay_api_token=None),
            payer_telegram_id=101, central_bot_id=102, invoice_id=uuid4(),
        )
    reserve.assert_not_awaited()


async def test_new_crypto_invoice_is_bound_to_original_frozen_order(monkeypatch):
    reserved = _reservation()
    provider = SimpleNamespace(
        provider_invoice_id=12345, bot_invoice_url="https://example.invalid/crypto",
    )
    create = AsyncMock(return_value=provider)
    bind = AsyncMock()
    reserve = AsyncMock(return_value=reserved)
    monkeypatch.setattr(gateway, "_reserve", reserve)
    monkeypatch.setattr(gateway, "create_platform_crypto_invoice", create)
    monkeypatch.setattr(gateway, "_bind", bind)
    invoice_id = uuid4()
    factory = object()

    link = await gateway.issue_platform_crypto_checkout(
        factory, settings=_settings(), payer_telegram_id=101,
        central_bot_id=102, invoice_id=invoice_id,
    )
    reserve.assert_awaited_once_with(
        factory, invoice_id=invoice_id, provider="crypto",
        payer_telegram_id=101, bot_id=102,
    )

    assert link == gateway.GatewayLink(
        "crypto", UUID(reserved["id"]),
        provider.bot_invoice_url, Decimal("150.00"), "USD", Decimal("5.00"),
    )
    assert create.await_args.kwargs["rub_amount"] == Decimal("150.00")
    assert create.await_args.kwargs["pay_amount"] == Decimal("5.00")
    assert create.await_args.kwargs["pay_currency"] == "USD"
    assert bind.await_args.kwargs["provider_id"] == "12345"
    assert bind.await_args.kwargs["url"] == provider.bot_invoice_url
    assert bind.await_args.kwargs["order_id"] == link.order_id


async def test_missing_lava_configuration_never_reserves_an_invoice(monkeypatch):
    reserve = AsyncMock()
    monkeypatch.setattr(gateway, "_reserve", reserve)
    with pytest.raises(gateway.LavaTopError, match="credentials/product"):
        await gateway.issue_platform_lava_checkout(
            object(), settings=_settings(saas_lava_top_offer_id=None),
            payer_telegram_id=101, central_bot_id=102,
            invoice_id=uuid4(), customer_email="payer@example.org",
        )
    reserve.assert_not_awaited()


async def test_lava_checkout_uses_exact_currency_and_customer_email(monkeypatch):
    reserved = _reservation()
    created = SimpleNamespace(
        invoice_id=uuid4(), payment_url="https://example.invalid/lava",
    )
    client = FakeLavaClient(created=created)
    bind = AsyncMock()
    monkeypatch.setattr(gateway, "LavaTopClient", lambda _api_key: client)
    reserve = AsyncMock(return_value=reserved)
    monkeypatch.setattr(gateway, "_reserve", reserve)
    monkeypatch.setattr(gateway, "_bind", bind)
    invoice_id = uuid4()
    factory = object()

    link = await gateway.issue_platform_lava_checkout(
        factory, settings=_settings(), payer_telegram_id=101,
        central_bot_id=102, invoice_id=invoice_id,
        customer_email="real-payer@example.org",
    )
    reserve.assert_awaited_once_with(
        factory, invoice_id=invoice_id, provider="lava",
        payer_telegram_id=101, bot_id=102,
    )
    assert link.provider == "lava"
    assert link.pay_amount == Decimal("5.00")
    assert link.amount_rub == Decimal("150.00")
    assert link.invoice_url == created.payment_url
    assert client.create_dynamic_invoice.await_args.kwargs == {
        "email": "real-payer@example.org",
        "offer_id": "test-offer", "amount": Decimal("5.00"),
        "currency": "USD",
    }
    assert bind.await_args.kwargs["provider_id"] == str(created.invoice_id)
    assert bind.await_args.kwargs["order_id"] == link.order_id


async def test_lava_provider_failure_does_not_expose_email_or_secrets(monkeypatch):
    client = FakeLavaClient(error=RuntimeError("private provider response"))
    monkeypatch.setattr(gateway, "LavaTopClient", lambda _api_key: client)
    monkeypatch.setattr(
        gateway, "_reserve", AsyncMock(return_value=_reservation()),
    )

    with pytest.raises(gateway.LavaTopError, match="creation unavailable") as error:
        await gateway.issue_platform_lava_checkout(
            object(), settings=_settings(), payer_telegram_id=101,
            central_bot_id=102, invoice_id=uuid4(),
            customer_email="private@example.org",
        )

    assert "private provider response" not in str(error.value)
    assert "private@example.org" not in str(error.value)
    assert error.value.__cause__ is None


async def test_lava_binding_failure_does_not_claim_success(monkeypatch):
    created = SimpleNamespace(
        invoice_id=uuid4(), payment_url="https://example.invalid/lava",
    )
    monkeypatch.setattr(
        gateway, "LavaTopClient",
        lambda _api_key: FakeLavaClient(created=created),
    )
    monkeypatch.setattr(
        gateway, "_reserve", AsyncMock(return_value=_reservation()),
    )
    monkeypatch.setattr(
        gateway, "_bind", AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(gateway.LavaTopError, match="binding failed") as error:
        await gateway.issue_platform_lava_checkout(
            object(), settings=_settings(), payer_telegram_id=101,
            central_bot_id=102, invoice_id=uuid4(),
            customer_email="real-payer@example.org",
        )

    assert error.value.__cause__ is None


@pytest.mark.parametrize("response", [None, "pending", {}, {"orders": []}])
async def test_pending_gateway_report_rejects_non_list_database_reply(
    monkeypatch, response,
):
    monkeypatch.setattr(gateway, "_rpc", AsyncMock(return_value=response))
    with pytest.raises(RuntimeError, match="pending platform gateway report"):
        await gateway.list_platform_pending_gateway_orders(
            object(), settings=_settings(),
        )


async def test_pending_gateway_report_preserves_order_and_operator_scope(monkeypatch):
    items = [{"id": "first"}, {"id": "second"}]
    rpc = AsyncMock(return_value=items)
    monkeypatch.setattr(gateway, "_rpc", rpc)
    assert await gateway.list_platform_pending_gateway_orders(
        object(), settings=_settings(), limit=25,
    ) == tuple(items)
    assert rpc.await_args.kwargs["telegram_id"] == 901
    assert rpc.await_args.kwargs["values"] == {"limit": 25}


@pytest.mark.parametrize("status", ["created", "pending", "cancelled", None])
async def test_unpaid_lava_receipts_never_reach_platform_ledger(
    monkeypatch, status,
):
    provider = FakeLavaClient(snapshot={"status": status})
    rpc = AsyncMock()
    verifier = Mock()
    monkeypatch.setattr(gateway, "LavaTopClient", lambda _api_key: provider)
    monkeypatch.setattr(gateway, "_rpc", rpc)
    monkeypatch.setattr(gateway, "verify_lava_paid_invoice", verifier)

    result = await gateway.verify_and_record_platform_gateway_payment(
        object(), settings=_settings(), pending=_pending(),
    )

    assert result is None
    rpc.assert_not_awaited()
    verifier.assert_not_called()


async def test_completed_lava_receipt_is_verified_before_ledger_posting(monkeypatch):
    pending = _pending()
    snapshot = {"status": "completed", "invoice_id": pending["provider_invoice_id"]}
    provider = FakeLavaClient(snapshot=snapshot)
    events = []
    verifier = Mock(side_effect=lambda *_args, **_kwargs: events.append("verified"))

    async def record_charge(_factory, **_kwargs):
        assert events == ["verified"], "Ledger write must follow provider verification"
        events.append("recorded")
        return {"applied": True}

    rpc = AsyncMock(side_effect=record_charge)
    monkeypatch.setattr(gateway, "LavaTopClient", lambda _api_key: provider)
    monkeypatch.setattr(gateway, "verify_lava_paid_invoice", verifier)
    monkeypatch.setattr(gateway, "_rpc", rpc)

    applied = await gateway.verify_and_record_platform_gateway_payment(
        object(), settings=_settings(), pending=pending,
    )

    assert applied == {"applied": True}
    assert events == ["verified", "recorded"]
    verifier.assert_called_once_with(
        snapshot, expected_id=UUID(pending["provider_invoice_id"]),
        expected_currency="USD", expected_amount=Decimal("5.00"),
    )
    rpc.assert_awaited_once()
    assert rpc.await_args.kwargs["name"] == "platform_saas_gateway_record_charge"
    assert rpc.await_args.kwargs["values"]["amount"] == Decimal("150.00")
    assert rpc.await_args.kwargs["values"]["provider_id"] == pending[
        "provider_invoice_id"
    ]
    stamp = rpc.await_args.kwargs["values"]["paid_at"]
    assert isinstance(stamp, datetime) and stamp.tzinfo == UTC


async def test_lava_mismatched_provider_receipt_cannot_post_ledger(monkeypatch):
    provider = FakeLavaClient(snapshot={"status": "completed"})
    rpc = AsyncMock()
    monkeypatch.setattr(gateway, "LavaTopClient", lambda _api_key: provider)
    monkeypatch.setattr(
        gateway, "verify_lava_paid_invoice",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("amount mismatch")),
    )
    monkeypatch.setattr(gateway, "_rpc", rpc)

    with pytest.raises(ValueError, match="amount mismatch"):
        await gateway.verify_and_record_platform_gateway_payment(
            object(), settings=_settings(), pending=_pending(),
        )
    rpc.assert_not_awaited()


async def test_unknown_payment_provider_never_creates_ledger_record(monkeypatch):
    rpc = AsyncMock()
    monkeypatch.setattr(gateway, "_rpc", rpc)
    with pytest.raises(ValueError, match="Invalid platform provider"):
        await gateway.verify_and_record_platform_gateway_payment(
            object(), settings=_settings(), pending=_pending(provider="other"),
        )
    rpc.assert_not_awaited()
