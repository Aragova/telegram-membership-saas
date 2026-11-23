"""Regression contracts for platform-owned gateway receipts and refunds.

All provider and PostgreSQL calls are mocked. No external charge is initiated.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules import platform_saas_gateway_service as gateway


def _settings(**overrides):
    defaults = {
        "telegram_central_access_enabled": False,
        "platform_owner_telegram_id": 90101,
        "saas_crypto_pay_api_token": "test-only-token",
        "saas_crypto_pay_testnet": False,
        "saas_lava_top_api_key": object(),
        "saas_lava_top_offer_id": "test-offer",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _reserved(**overrides):
    payload = {
        "id": str(uuid4()),
        "status": "reserved",
        "amount_rub": "150.00",
        "pay_currency": "RUB",
        "pay_amount": "150.00",
    }
    payload.update(overrides)
    return payload


def _pending(**overrides):
    payload = {
        "id": str(uuid4()),
        "provider": "crypto",
        "provider_invoice_id": "1234",
        "invoice_payload": "unique-order",
        "amount_rub": "150.00",
        "pay_currency": "RUB",
        "pay_amount": "150.00",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("central", "bot_id"),
    [(True, 3), (False, 0), (False, -1)],
)
def test_external_checkout_requires_central_bot(central, bot_id):
    with pytest.raises(PermissionError, match="central bot"):
        gateway._central_only(_settings(telegram_central_access_enabled=central), bot_id=bot_id)


@pytest.mark.parametrize(
    ("central", "owner"),
    [(True, 1), (False, None), (False, 0), (False, -1)],
)
def test_settlement_requires_actual_platform_operator(central, owner):
    with pytest.raises(PermissionError, match="platform owner"):
        gateway._platform_operator(
            _settings(
                telegram_central_access_enabled=central,
                platform_owner_telegram_id=owner,
            )
        )


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {},
        _reserved(id=123),
        _reserved(status="cancelled"),
        _reserved(amount_rub="0"),
        _reserved(pay_currency="EUR"),
        _reserved(pay_amount="0"),
    ],
)
async def test_reservation_rejects_nonpayable_or_unauthorized_quote(monkeypatch, reply):
    rpc = AsyncMock(return_value=reply)
    monkeypatch.setattr(gateway, "_rpc", rpc)

    with pytest.raises(RuntimeError, match="Invalid platform gateway reservation"):
        await gateway._reserve(
            object(),
            invoice_id=uuid4(),
            provider="crypto",
            payer_telegram_id=12345,
            bot_id=67890,
        )


async def test_reservation_retains_exact_original_quote(monkeypatch):
    reservation = _reserved(pay_currency="USD", pay_amount="5.00")
    expected = reservation.copy()
    rpc = AsyncMock(return_value=reservation)
    monkeypatch.setattr(gateway, "_rpc", rpc)
    invoice_id = uuid4()

    actual = await gateway._reserve(
        object(), invoice_id=invoice_id, provider="lava",
        payer_telegram_id=101, bot_id=202,
    )

    assert actual == expected
    assert rpc.await_args.kwargs["values"] == {
        "invoice": invoice_id,
        "provider": "lava",
        "telegram_id": 101,
        "bot_id": 202,
    }


async def test_bound_gateway_invoice_must_be_confirmed_by_database(monkeypatch):
    rpc = AsyncMock(return_value=False)
    monkeypatch.setattr(gateway, "_rpc", rpc)

    with pytest.raises(RuntimeError, match="not bound"):
        await gateway._bind(
            object(), settings=_settings(), order_id=uuid4(),
            payer_telegram_id=101, bot_id=202,
            provider_id="existing", url="https://example.invalid/invoice",
        )

    assert rpc.await_args.kwargs["telegram_id"] == 90101


async def test_existing_crypto_invoice_reused_without_provider_charge(monkeypatch):
    order_id = uuid4()
    reservation = _reserved(
        id=str(order_id), status="issued",
        url="https://example.invalid/existing", pay_currency="USD",
        pay_amount="5.00",
    )
    monkeypatch.setattr(gateway, "_reserve", AsyncMock(return_value=reservation))
    create = AsyncMock()
    monkeypatch.setattr(gateway, "create_platform_crypto_invoice", create)
    monkeypatch.setattr(gateway, "_invoice_link", lambda url, testnet: url)

    link = await gateway.issue_platform_crypto_checkout(
        object(), settings=_settings(), payer_telegram_id=101,
        central_bot_id=202, invoice_id=uuid4(),
    )

    assert link.provider == "crypto"
    assert link.order_id == order_id
    assert link.invoice_url == reservation["url"]
    assert link.amount_rub == Decimal("150.00")
    assert link.pay_currency == "USD"
    assert link.pay_amount == Decimal("5.00")
    create.assert_not_awaited()


async def test_crypto_existing_invoice_without_link_is_not_reissued(monkeypatch):
    monkeypatch.setattr(
        gateway, "_reserve",
        AsyncMock(return_value=_reserved(status="issued")),
    )
    create = AsyncMock()
    monkeypatch.setattr(gateway, "create_platform_crypto_invoice", create)

    with pytest.raises(RuntimeError, match="link is unavailable"):
        await gateway.issue_platform_crypto_checkout(
            object(), settings=_settings(), payer_telegram_id=101,
            central_bot_id=202, invoice_id=uuid4(),
        )
    create.assert_not_awaited()


async def test_existing_lava_invoice_preserves_original_provider_order(monkeypatch):
    order_id = uuid4()
    reservation = _reserved(
        id=str(order_id), status="issued", pay_currency="USD",
        pay_amount="5.00", url="https://example.invalid/invoice",
    )
    monkeypatch.setattr(gateway, "_reserve", AsyncMock(return_value=reservation))
    monkeypatch.setattr(gateway, "_checkout_link", lambda url: url)

    link = await gateway.issue_platform_lava_checkout(
        object(), settings=_settings(), payer_telegram_id=101,
        central_bot_id=202, invoice_id=uuid4(), customer_email="payer@example.org",
    )

    assert link == gateway.GatewayLink(
        "lava", order_id, reservation["url"], Decimal("150.00"),
        "USD", Decimal("5.00"),
    )


async def test_lava_reservation_failure_does_not_expose_internal_details(monkeypatch):
    monkeypatch.setattr(
        gateway, "_reserve", AsyncMock(side_effect=RuntimeError("secret database text")),
    )

    with pytest.raises(gateway.LavaTopError, match="could not be reserved") as error:
        await gateway.issue_platform_lava_checkout(
            object(), settings=_settings(), payer_telegram_id=101,
            central_bot_id=202, invoice_id=uuid4(), customer_email="payer@example.org",
        )
    assert "secret database text" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    ("currency", "amount"),
    [
        ("EUR", "5.00"),
        ("USD", "0.00"),
        ("RUB", "-1.00"),
        ("USD", "5.001"),
        ("USD", "NaN"),
        ("RUB", "Infinity"),
    ],
)
async def test_gateway_reconciliation_rejects_invalid_frozen_quote(
    monkeypatch, currency, amount,
):
    lookup = AsyncMock()
    monkeypatch.setattr(gateway, "lookup_platform_crypto_payment", lookup)

    with pytest.raises(ValueError, match="immutable gateway payment quote"):
        await gateway.verify_and_record_platform_gateway_payment(
            object(), settings=_settings(),
            pending=_pending(pay_currency=currency, pay_amount=amount),
        )
    lookup.assert_not_awaited()


async def test_unpaid_crypto_invoice_never_creates_ledger_charge(monkeypatch):
    lookup = AsyncMock(return_value=None)
    record = AsyncMock()
    monkeypatch.setattr(gateway, "lookup_platform_crypto_payment", lookup)
    monkeypatch.setattr(gateway, "_rpc", record)

    result = await gateway.verify_and_record_platform_gateway_payment(
        object(), settings=_settings(), pending=_pending(),
    )

    assert result is None
    record.assert_not_awaited()


async def test_paid_crypto_invoice_records_exact_frozen_amount(monkeypatch):
    stamp = datetime(2026, 11, 1, tzinfo=UTC)
    pending = _pending(pay_currency="USD", pay_amount="5.00")
    lookup = AsyncMock(return_value=SimpleNamespace(paid_at=stamp))
    rpc = AsyncMock(return_value={"applied": True})
    monkeypatch.setattr(gateway, "lookup_platform_crypto_payment", lookup)
    monkeypatch.setattr(gateway, "_rpc", rpc)

    result = await gateway.verify_and_record_platform_gateway_payment(
        object(), settings=_settings(), pending=pending,
    )

    assert result == {"applied": True}
    assert lookup.await_args.kwargs["pay_amount"] == Decimal("5.00")
    assert rpc.await_args.kwargs["values"]["amount"] == Decimal("150.00")
    assert rpc.await_args.kwargs["values"]["paid_at"] == stamp
    assert rpc.await_args.kwargs["telegram_id"] == 90101


async def test_gateway_does_not_accept_invalid_ledger_acknowledgement(monkeypatch):
    stamp = datetime(2026, 11, 1, tzinfo=UTC)
    monkeypatch.setattr(
        gateway, "lookup_platform_crypto_payment",
        AsyncMock(return_value=SimpleNamespace(paid_at=stamp)),
    )
    monkeypatch.setattr(gateway, "_rpc", AsyncMock(return_value={"applied": "yes"}))

    with pytest.raises(RuntimeError, match="Invalid platform gateway settlement"):
        await gateway.verify_and_record_platform_gateway_payment(
            object(), settings=_settings(), pending=_pending(),
        )


@pytest.mark.parametrize(
    ("provider", "currency", "ref", "refund_id", "amount"),
    [
        ("unknown", "RUB", "invoice", "refund", Decimal("10.00")),
        ("lava", "EUR", "invoice", "refund", Decimal("10.00")),
        ("lava", "RUB", "", "refund", Decimal("10.00")),
        ("lava", "RUB", "with space", "refund", Decimal("10.00")),
        ("lava", "RUB", "invoice", "with space", Decimal("10.00")),
        ("lava", "RUB", "invoice", "refund", Decimal("0")),
        ("lava", "RUB", "invoice", "refund", Decimal("NaN")),
        ("lava", "RUB", "invoice", "refund", Decimal("1.001")),
    ],
)
async def test_operator_refund_requires_verified_well_formed_evidence(
    monkeypatch, provider, currency, ref, refund_id, amount,
):
    rpc = AsyncMock()
    monkeypatch.setattr(gateway, "_rpc", rpc)
    with pytest.raises(ValueError):
        await gateway.record_operator_verified_gateway_refund(
            object(), settings=_settings(), provider=provider,
            currency=currency, provider_invoice_id=ref,
            provider_refund_id=refund_id, amount_rub=amount,
        )
    rpc.assert_not_awaited()


@pytest.mark.parametrize("currency", ["RUB", "USD"])
async def test_refund_uses_currency_specific_owner_ledger_rpc(monkeypatch, currency):
    ack = {
        "recorded": True, "amount_rub": "150.00", "credit_reversed_rub": "150.00",
    }
    if currency == "USD":
        ack.update(currency="USD", amount_native="5.00")
    rpc = AsyncMock(return_value=ack)
    monkeypatch.setattr(gateway, "_rpc", rpc)

    native_refund_amount = Decimal("5.00" if currency == "USD" else "150.00")
    result = await gateway.record_operator_verified_gateway_refund(
        object(), settings=_settings(), provider="lava",
        currency=currency, provider_invoice_id="verified-order",
        provider_refund_id="verified-refund", amount_rub=native_refund_amount,
    )

    assert result == ack
    assert rpc.await_args.kwargs["values"]["amount"] == native_refund_amount
    assert rpc.await_args.kwargs["name"] == (
        "platform_saas_gateway_operator_refund_native"
        if currency == "USD"
        else "platform_saas_gateway_operator_refund"
    )
    assert rpc.await_args.kwargs["telegram_id"] == 90101
    assert ("currency" in rpc.await_args.kwargs["values"]) == (currency == "USD")


async def test_refund_rejects_incomplete_database_acknowledgement(monkeypatch):
    monkeypatch.setattr(
        gateway, "_rpc",
        AsyncMock(return_value={"recorded": True, "amount_rub": "150.00"}),
    )
    with pytest.raises(RuntimeError, match="ledger acknowledgement invalid"):
        await gateway.record_operator_verified_gateway_refund(
            object(), settings=_settings(), provider="crypto",
            provider_invoice_id="invoice", provider_refund_id="refund",
            amount_rub=Decimal("150.00"),
        )
