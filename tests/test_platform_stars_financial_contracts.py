"""Regression tests for platform-only Telegram Stars accounting.

No real Telegram charge, refund, operator credentials or database changes.
The tests pin provider identity, idempotence and quote/policy boundaries.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules import platform_saas_stars_service as stars


@pytest.fixture
def rpc(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(stars, "_rpc", call)
    return call


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setattr(
        "telegram_membership_saas.config.get_settings",
        lambda: object(),
    )
    monkeypatch.setattr(stars, "_check_actor", lambda *_: None)


def _quote(**changes):
    data = {
        "payload": "vps:" + str(uuid4()),
        "stars_amount": 405,
        "rub_amount": "150.00",
        "usd_quote": "5.00",
        "usd_per_star": "0.013",
        "withdrawal_reserve_pct": "5.00",
    }
    data.update(changes)
    return data


def _charge_kwargs(**changes):
    values = {
        "payer_telegram_id": 501,
        "central_bot_id": 601,
        "payload": "vps:" + str(uuid4()),
        "currency": stars.STARS_CURRENCY,
        "stars_amount": 405,
        "charge_id": "charge-123456",
        "paid_at": datetime(2026, 11, 24, tzinfo=UTC),
    }
    values.update(changes)
    return values


@pytest.mark.parametrize(
    ("operation", "arguments", "payer"),
    [
        ("unknown_stars_operation", {}, 501),
        ("platform_saas_stars_create_checkout", {"invoice": uuid4()}, 501),
        ("platform_saas_stars_rate", {"injected": "other-user"}, 501),
        ("platform_saas_stars_rate", {}, 0),
    ],
)
async def test_stars_rpc_closed_set_rejects_untrusted_calls(
    monkeypatch, operation, arguments, payer,
):
    identity = AsyncMock()
    monkeypatch.setattr(stars, "ensure_telegram_actor", identity)
    with pytest.raises(ValueError):
        await stars._rpc(
            object(), payer_telegram_id=payer, name=operation,
            arguments=arguments,
        )
    identity.assert_not_awaited()


async def test_stars_order_rejects_invalid_central_bot_before_db(rpc):
    with pytest.raises(ValueError, match="Invalid central bot identity"):
        await stars.create_platform_stars_order(
            object(), invoice_id=uuid4(), payer_telegram_id=501, central_bot_id=0,
        )
    rpc.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"payload": "not-platform-invoice"},
        {"payload": None},
        {"stars_amount": 0},
        {"stars_amount": stars.STARS_LIMIT + 1},
        {"stars_amount": "405"},
        {"rub_amount": "0"},
        {"usd_quote": "-1"},
        {"usd_per_star": "0"},
        {"withdrawal_reserve_pct": "-1"},
        {"withdrawal_reserve_pct": "31"},
    ],
)
async def test_stars_order_rejects_unpayable_or_malformed_quote(rpc, changes):
    rpc.return_value = _quote(**changes)
    with pytest.raises(RuntimeError, match="Invalid platform Stars quote"):
        await stars.create_platform_stars_order(
            object(), invoice_id=uuid4(), payer_telegram_id=501, central_bot_id=601,
        )


async def test_stars_order_preserves_frozen_quote_and_original_payer(rpc):
    original = _quote()
    expected = original.copy()
    rpc.return_value = original
    invoice_id = uuid4()
    factory = object()
    result = await stars.create_platform_stars_order(
        factory, invoice_id=invoice_id, payer_telegram_id=501, central_bot_id=601,
    )
    assert result == expected
    rpc.assert_awaited_once_with(
        factory, payer_telegram_id=501,
        name="platform_saas_stars_create_checkout",
        arguments={"invoice": invoice_id, "telegram_id": 501, "bot_id": 601},
    )


@pytest.mark.parametrize(
    ("currency", "payload", "amount"),
    [
        ("RUB", "vps:paid", 5),
        ("USD", "vps:paid", 5),
        ("XTR", "other:paid", 5),
        ("XTR", "vps:paid", 0),
        ("XTR", "vps:paid", stars.STARS_LIMIT + 1),
    ],
)
async def test_precheckout_rejects_wrong_provider_evidence_before_rpc(
    rpc, currency, payload, amount,
):
    assert not await stars.platform_stars_precheckout(
        object(), payer_telegram_id=501, central_bot_id=601,
        payload=payload, currency=currency, stars_amount=amount,
    )
    rpc.assert_not_awaited()


@pytest.mark.parametrize("provider_answer", [True, False, None, 1, "true"])
async def test_precheckout_returns_only_literal_database_approval(
    rpc, provider_answer,
):
    rpc.return_value = provider_answer
    result = await stars.platform_stars_precheckout(
        object(), payer_telegram_id=501, central_bot_id=601,
        payload="vps:pending", currency=stars.STARS_CURRENCY, stars_amount=405,
    )
    assert result is (provider_answer is True)
    assert rpc.await_args.kwargs["arguments"] == {
        "payload": "vps:pending",
        "telegram_id": 501, "bot_id": 601, "amount": 405,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "RUB"},
        {"payload": "not-vps:charge"},
        {"stars_amount": 0},
        {"stars_amount": stars.STARS_LIMIT + 1},
        {"charge_id": "short"},
        {"charge_id": "X" * 256},
        {"paid_at": datetime(2026, 11, 24)},
    ],
)
async def test_successful_payment_rejects_unverified_provider_fields(
    rpc, changes,
):
    with pytest.raises(ValueError, match="provider-authenticated Stars payment"):
        await stars.confirm_platform_stars_charge(
            object(), **_charge_kwargs(**changes),
        )
    rpc.assert_not_awaited()


@pytest.mark.parametrize("applied", [True, False])
async def test_stars_charge_records_original_identity_and_idempotent_result(
    rpc, applied,
):
    rpc.return_value = {"applied": applied}
    values = _charge_kwargs()
    factory = object()
    result = await stars.confirm_platform_stars_charge(factory, **values)
    assert result == {"applied": applied}
    rpc.assert_awaited_once_with(
        factory, payer_telegram_id=501,
        name="platform_saas_stars_record_charge",
        arguments={
            "payload": values["payload"], "telegram_id": 501, "bot_id": 601,
            "amount": 405, "charge_id": values["charge_id"],
            "paid_at": values["paid_at"],
        },
    )


@pytest.mark.parametrize("db_result", [None, {}, {"applied": 1}, "paid"])
async def test_stars_charge_does_not_accept_invalid_ledger_response(
    rpc, db_result,
):
    rpc.return_value = db_result
    with pytest.raises(RuntimeError, match="verified platform Stars settlement"):
        await stars.confirm_platform_stars_charge(object(), **_charge_kwargs())


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "USD"},
        {"payload": "incorrect-prefix"},
        {"stars_amount": 0},
        {"stars_amount": stars.STARS_LIMIT + 1},
        {"charge_id": "short"},
        {"refunded_at": datetime(2026, 11, 24)},
    ],
)
async def test_stars_refund_rejects_unverified_provider_fields(rpc, changes):
    values = _charge_kwargs()
    values["refunded_at"] = values.pop("paid_at")
    values.update(changes)
    with pytest.raises(ValueError, match="provider-authenticated Stars refund"):
        await stars.record_platform_stars_refund(object(), **values)
    rpc.assert_not_awaited()


@pytest.mark.parametrize("db_answer", [True, False, 1, None])
async def test_stars_refund_returns_only_database_confirmed_result(rpc, db_answer):
    rpc.return_value = db_answer
    values = _charge_kwargs()
    values["refunded_at"] = values.pop("paid_at")
    factory = object()
    answer = await stars.record_platform_stars_refund(factory, **values)
    assert answer is (db_answer is True)
    rpc.assert_awaited_once_with(
        factory, payer_telegram_id=501,
        name="platform_saas_stars_record_refund",
        arguments={
            "payload": values["payload"], "telegram_id": 501, "bot_id": 601,
            "amount": 405, "charge_id": values["charge_id"],
            "refunded_at": values["refunded_at"],
        },
    )


@pytest.mark.parametrize("charge", ["short", "X" * 256])
async def test_recover_charge_rejects_invalid_reference(rpc, charge):
    with pytest.raises(ValueError, match="Telegram charge reference"):
        await stars.recover_platform_stars_charge_payload(
            object(), payer_telegram_id=501, central_bot_id=601, charge_id=charge,
        )
    rpc.assert_not_awaited()


@pytest.mark.parametrize("db_payload", [None, "vps:original", "not-vimi", 123])
async def test_recover_charge_never_uses_unverified_invoice_payload(rpc, db_payload):
    rpc.return_value = db_payload
    args = dict(payer_telegram_id=501, central_bot_id=601, charge_id="charge-123456")
    if db_payload is not None and db_payload != "vps:original":
        with pytest.raises(RuntimeError, match="original platform invoice payload"):
            await stars.recover_platform_stars_charge_payload(object(), **args)
    else:
        assert await stars.recover_platform_stars_charge_payload(object(), **args) == db_payload
    assert rpc.await_args.kwargs["arguments"] == {
        "charge_id": args["charge_id"], "telegram_id": 501, "bot_id": 601,
    }


@pytest.mark.parametrize(
    "invalid",
    ["NaN", "Infinity", "-1", "0", "1001", "0.000000001"],
)
async def test_rub_star_rate_validation_happens_before_operator_rpc(
    rpc, operator, invalid,
):
    with pytest.raises(ValueError, match="Invalid RUB-per-Star payout rate"):
        await stars.set_platform_stars_rate(
            object(), operator_telegram_id=501, rub_per_star=Decimal(invalid),
        )
    rpc.assert_not_awaited()


async def test_operator_rate_preserves_exact_decimal_value(rpc, operator):
    rpc.return_value = "2.10000000"
    factory = object()
    result = await stars.set_platform_stars_rate(
        factory, operator_telegram_id=501, rub_per_star=Decimal("2.10000000"),
    )
    assert result == Decimal("2.10000000")
    rpc.assert_awaited_once_with(
        factory, payer_telegram_id=501, name="platform_saas_stars_set_rate",
        arguments={"rate": Decimal("2.10000000")},
    )


@pytest.mark.parametrize("db_rate", [None, "0.01300000"])
async def test_operator_rate_reader_distinguishes_missing_from_frozen_value(
    rpc, operator, db_rate,
):
    rpc.return_value = db_rate
    factory = object()
    assert await stars.platform_stars_rate(
        factory, operator_telegram_id=501,
    ) == (None if db_rate is None else Decimal(db_rate))
    rpc.assert_awaited_once_with(
        factory, payer_telegram_id=501,
        name="platform_saas_stars_rate", arguments={},
    )


@pytest.mark.parametrize(
    "policy",
    [
        None, {},
        {"enabled": "yes", "usd_per_star": "0.013", "withdrawal_reserve_pct": "5",
         "example_stars": 405},
        {"enabled": True, "usd_per_star": "0", "withdrawal_reserve_pct": "5",
         "example_stars": 405},
        {"enabled": True, "usd_per_star": "0.013", "withdrawal_reserve_pct": "31",
         "example_stars": 405},
        {"enabled": True, "usd_per_star": "0.013", "withdrawal_reserve_pct": "5",
         "example_stars": None},
    ],
)
async def test_stars_usd_policy_reader_rejects_malformed_authoritative_values(
    rpc, operator, policy,
):
    rpc.return_value = policy
    with pytest.raises(RuntimeError, match="Stars USD policy"):
        await stars.platform_stars_usd_policy(object(), operator_telegram_id=501)


@pytest.mark.parametrize(
    ("usd", "reserve", "enabled"),
    [
        ("NaN", "5.00", True),
        ("0", "5.00", True),
        ("0.051", "5.00", True),
        ("0.013000001", "5.00", True),
        ("0.013", "NaN", True),
        ("0.013", "-1", True),
        ("0.013", "30.01", True),
        ("0.013", "5.001", True),
        ("0.013", "5.00", "true"),
    ],
)
async def test_usd_policy_set_rejects_invalid_future_quote(
    rpc, operator, usd, reserve, enabled,
):
    with pytest.raises(ValueError, match="USD Stars reward and reserve"):
        await stars.configure_platform_stars_usd_policy(
            object(), operator_telegram_id=501, usd_per_star=Decimal(usd),
            reserve_percent=Decimal(reserve), enabled=enabled,
        )
    rpc.assert_not_awaited()


@pytest.mark.parametrize("enabled", [True, False])
async def test_usd_policy_requires_matching_db_acknowledgement(
    rpc, operator, enabled,
):
    rpc.return_value = {"enabled": not enabled}
    with pytest.raises(RuntimeError, match="Stars USD policy acknowledgement"):
        await stars.configure_platform_stars_usd_policy(
            object(), operator_telegram_id=501,
            usd_per_star=Decimal("0.013"),
            reserve_percent=Decimal("5.00"), enabled=enabled,
        )
    assert rpc.await_args.kwargs["arguments"] == {
        "usd_per_star": Decimal("0.013"),
        "reserve_pct": Decimal("5.00"),
        "enabled": enabled,
    }


async def test_usd_policy_accepts_operator_approved_immutable_parameters(
    rpc, operator,
):
    result = {
        "enabled": True, "usd_per_star": "0.013", "withdrawal_reserve_pct": "5.00",
    }
    rpc.return_value = result
    factory = object()
    assert await stars.configure_platform_stars_usd_policy(
        factory, operator_telegram_id=501,
        usd_per_star=Decimal("0.013"), reserve_percent=Decimal("5.00"),
        enabled=True,
    ) == result
    rpc.assert_awaited_once_with(
        factory, payer_telegram_id=501,
        name="platform_saas_stars_usd_policy_set",
        arguments={
            "usd_per_star": Decimal("0.013"),
            "reserve_pct": Decimal("5.00"), "enabled": True,
        },
    )
