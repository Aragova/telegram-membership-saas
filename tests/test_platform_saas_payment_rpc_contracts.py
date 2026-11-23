"""No-network validation tests for ViMi Pay payment proof and invoice RPCs.

The real SQL/RLS integration tests remain separate. This suite verifies that
bad evidence and malformed quotes never reach a SECURITY DEFINER call.
"""

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules import platform_saas_v2_payment_service as service


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("invented_payment_operation", {}),
        ("platform_saas_v2_create_attempt", {"invoice": uuid4()}),
        ("platform_saas_v2_methods_read", {"injected": True}),
        ("platform_saas_v2_submit_proof", {"file_unique_id": "fake"}),
    ],
)
async def test_closed_set_rpc_rejects_unexpected_operation_or_fields(name, arguments):
    with pytest.raises(ValueError, match="Unknown ViMi Pay billing operation"):
        await service._rpc(
            object(), actor_id=uuid4(), name=name, arguments=arguments,
        )


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        (Decimal("0"), "RUB"),
        (Decimal("-1"), "USD"),
        (Decimal("NaN"), "RUB"),
        (Decimal("Infinity"), "RUB"),
        (Decimal("1.001"), "RUB"),
        (Decimal("5.00"), "XTR"),
    ],
)
async def test_legacy_bank_attempt_blocks_nonpayable_quotes(
    monkeypatch, amount, currency,
):
    rpc = AsyncMock()
    monkeypatch.setattr(service, "_rpc", rpc)

    with pytest.raises(ValueError):
        await service.saas_v2_create_attempt(
            object(), actor_id=uuid4(), invoice_id=uuid4(),
            rub=amount, currency=currency,
        )
    rpc.assert_not_awaited()


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        (Decimal("0"), "USD"),
        (Decimal("-0.01"), "RUB"),
        (Decimal("NaN"), "RUB"),
        (Decimal("Infinity"), "USD"),
        (Decimal("2.001"), "RUB"),
        (Decimal("5.00"), "XTR"),
    ],
)
async def test_catalog_bank_attempt_blocks_nonpayable_quotes(
    monkeypatch, amount, currency,
):
    rpc = AsyncMock()
    monkeypatch.setattr(service, "_rpc", rpc)

    with pytest.raises(ValueError):
        await service.saas_v3_create_attempt(
            object(), actor_id=uuid4(), invoice_id=uuid4(),
            rub=amount, currency=currency, method_id=uuid4(),
        )
    rpc.assert_not_awaited()


async def test_valid_bank_attempt_uses_original_invoice_and_currency(monkeypatch):
    invoice = uuid4()
    owner = uuid4()
    rpc = AsyncMock(return_value={"id": str(uuid4())})
    monkeypatch.setattr(service, "_rpc", rpc)

    created = await service.saas_v2_create_attempt(
        object(), actor_id=owner, invoice_id=invoice,
        rub=Decimal("150.00"), currency="RUB",
    )

    assert "id" in created
    assert rpc.await_args.kwargs["actor_id"] == owner
    assert rpc.await_args.kwargs["name"] == "platform_saas_v2_create_attempt"
    assert rpc.await_args.kwargs["arguments"] == {
        "invoice": invoice, "amount_rub": Decimal("150.00"), "currency": "RUB",
    }


@pytest.mark.parametrize(
    ("bot", "chat", "message", "file_id", "kind", "unique"),
    [
        (0, 5, 5, "A", "photo", None),
        (5, -1, 5, "A", "photo", None),
        (5, 5, 0, "A", "photo", None),
        (5, 5, 5, "", "photo", None),
        (5, 5, 5, "A", "video", None),
        (5, 5, 5, "A", "document", "short"),
        (5, 5, 5, "A", "photo", "invalid unique id"),
        (5, 5, 5, "A", "photo", "invalid*unique"),
        (5, 5, 5, "A", "photo", "abc12345é"),
    ],
)
async def test_payment_proof_rejects_untrusted_source_or_canonical_id(
    monkeypatch, bot, chat, message, file_id, kind, unique,
):
    rpc = AsyncMock()
    monkeypatch.setattr(service, "_rpc", rpc)

    with pytest.raises((PermissionError, ValueError)):
        await service.saas_v2_submit_proof(
            object(), actor_id=uuid4(), attempt_id=uuid4(),
            project_id=None, bot_id=bot, chat_id=chat,
            message_id=message, file_id=file_id, file_type=kind,
            file_unique_id=unique,
        )
    rpc.assert_not_awaited()


@pytest.mark.parametrize("unique", [None, "ABCD1234-safe_identity"])
async def test_valid_payment_proof_preserves_original_telegram_evidence(
    monkeypatch, unique,
):
    attempt = uuid4()
    proof = uuid4()
    rpc = AsyncMock(return_value=str(proof))
    monkeypatch.setattr(service, "_rpc", rpc)

    result = await service.saas_v2_submit_proof(
        object(), actor_id=uuid4(), attempt_id=attempt, project_id=None,
        bot_id=101, chat_id=102, message_id=103,
        file_id="file_reference", file_type="document",
        file_unique_id=unique,
    )

    args = rpc.await_args.kwargs["arguments"]
    assert result == proof
    assert args["attempt"] == attempt
    assert args["bot_id"] == 101
    assert args["chat_id"] == 102
    assert args["message_id"] == 103
    assert args["file_id"] == "file_reference"
    assert ("file_unique_id" in args) == (unique is not None)


@pytest.mark.parametrize(
    "currency",
    ["EUR", "BYN", "UAH", "XTR", "", "USDT"],
)
async def test_service_price_profile_disallows_unsupported_monthly_currency(
    monkeypatch, currency,
):
    rpc = AsyncMock()
    monkeypatch.setattr(service, "_rpc", rpc)
    with pytest.raises(ValueError, match="Unknown ViMi Pay monthly service price"):
        await service.saas_price_profile(
            object(), actor_id=uuid4(), currency=currency,
        )
    rpc.assert_not_awaited()


@pytest.mark.parametrize(
    ("project", "currency", "expected"),
    [
        (False, None, "platform_saas_price_profile_read"),
        (False, "USD", "platform_saas_price_profile_set"),
        (True, None, "platform_saas_bot_price_profile_read_v2"),
        (True, "RUB", "platform_saas_bot_price_profile_set"),
    ],
)
async def test_price_profile_preserves_bot_vs_account_scope(
    monkeypatch, project, currency, expected,
):
    rpc = AsyncMock(return_value={"currency": currency or "RUB"})
    monkeypatch.setattr(service, "_rpc", rpc)
    project_id = uuid4() if project else None

    result = await service.saas_price_profile(
        object(), actor_id=uuid4(), currency=currency,
        project_id=project_id,
    )

    assert result["currency"] in ("RUB", "USD")
    assert rpc.await_args.kwargs["name"] == expected
    assert rpc.await_args.kwargs["project_id"] == project_id
    if project_id is not None:
        assert rpc.await_args.kwargs["arguments"]["project"] == project_id


@pytest.mark.parametrize(
    "reply",
    [None, {}, {"currency": "EUR"}, {"currency": "XTR"}, "RUB"],
)
async def test_service_price_profile_rejects_malformed_database_response(
    monkeypatch, reply,
):
    monkeypatch.setattr(service, "_rpc", AsyncMock(return_value=reply))
    with pytest.raises(RuntimeError, match="Invalid ViMi Pay service price"):
        await service.saas_price_profile(object(), actor_id=uuid4())


@pytest.mark.parametrize(
    "reply",
    [None, {}, ["RUB", "XTR"], ["USD", 123], "RUB"],
)
async def test_payment_currency_list_rejects_invalid_database_response(
    monkeypatch, reply,
):
    monkeypatch.setattr(service, "_rpc", AsyncMock(return_value=reply))
    with pytest.raises(RuntimeError, match="Invalid platform bank currency list"):
        await service.saas_v2_available_methods(
            object(), actor_id=uuid4(), invoice_id=uuid4(),
        )


async def test_allowed_bank_currencies_are_returned_without_reordering(monkeypatch):
    currencies = ["BYN", "USD", "RUB"]
    monkeypatch.setattr(service, "_rpc", AsyncMock(return_value=currencies))
    actual = await service.saas_v2_available_methods(
        object(), actor_id=uuid4(), invoice_id=uuid4(),
    )
    assert actual == tuple(currencies)


@pytest.mark.parametrize("page", [-1, 100001])
async def test_proof_review_page_is_bounded_before_query(monkeypatch, page):
    rpc = AsyncMock()
    monkeypatch.setattr(service, "_rpc", rpc)
    monkeypatch.setattr(service, "_check_actor", lambda telegram_id, settings: None)
    with pytest.raises(ValueError, match="Invalid payment review page"):
        await service.saas_v2_pending_proofs(
            object(), actor_id=uuid4(), actor_telegram_id=123, page=page,
        )
    rpc.assert_not_awaited()


async def test_proof_review_rejects_malformed_queue(monkeypatch):
    monkeypatch.setattr(service, "_rpc", AsyncMock(return_value={"items": None}))
    monkeypatch.setattr(service, "_check_actor", lambda telegram_id, settings: None)
    with pytest.raises(RuntimeError, match="Invalid bank review queue"):
        await service.saas_v2_pending_proofs(
            object(), actor_id=uuid4(), actor_telegram_id=123,
        )


async def test_operator_cannot_open_nonexistent_receipt_file(monkeypatch):
    monkeypatch.setattr(service, "_rpc", AsyncMock(return_value={"source_bot": 1}))
    monkeypatch.setattr(service, "_check_actor", lambda telegram_id, settings: None)
    with pytest.raises(ValueError, match="Pending receipt source unavailable"):
        await service.saas_pending_proof_source(
            object(), actor_id=uuid4(), actor_telegram_id=123,
            attempt_id=uuid4(),
        )


@pytest.mark.parametrize("reference", ["", "short", "   ", "A" * 161])
async def test_bank_approval_requires_independent_reference(
    monkeypatch, reference,
):
    rpc = AsyncMock()
    monkeypatch.setattr(service, "_rpc", rpc)
    monkeypatch.setattr(service, "_check_actor", lambda telegram_id, settings: None)

    with pytest.raises(ValueError, match="verified bank reference"):
        await service.saas_v2_confirm_attempt(
            object(), actor_id=uuid4(), actor_telegram_id=123,
            attempt_id=uuid4(), verified_bank_reference=reference,
        )
    rpc.assert_not_awaited()


async def test_bank_approval_strips_reference_before_ledger_write(monkeypatch):
    attempt = uuid4()
    rpc = AsyncMock(return_value=str(attempt))
    monkeypatch.setattr(service, "_rpc", rpc)
    monkeypatch.setattr(service, "_check_actor", lambda telegram_id, settings: None)

    result = await service.saas_v2_confirm_attempt(
        object(), actor_id=uuid4(), actor_telegram_id=123,
        attempt_id=attempt, verified_bank_reference="  BANK-1234567  ",
    )

    assert result == attempt
    assert rpc.await_args.kwargs["arguments"]["bank_reference"] == "BANK-1234567"


@pytest.mark.parametrize("reason", ["", "no", "    ", "R" * 501])
async def test_bank_rejection_requires_audit_reason(monkeypatch, reason):
    rpc = AsyncMock()
    monkeypatch.setattr(service, "_rpc", rpc)
    monkeypatch.setattr(service, "_check_actor", lambda telegram_id, settings: None)

    with pytest.raises(ValueError, match="Review reason required"):
        await service.saas_v2_reject_attempt(
            object(), actor_id=uuid4(), actor_telegram_id=123,
            attempt_id=uuid4(), reason=reason,
        )
    rpc.assert_not_awaited()


async def test_bank_rejection_records_stripped_reason(monkeypatch):
    attempt = uuid4()
    rpc = AsyncMock(return_value=str(attempt))
    monkeypatch.setattr(service, "_rpc", rpc)
    monkeypatch.setattr(service, "_check_actor", lambda telegram_id, settings: None)
    result = await service.saas_v2_reject_attempt(
        object(), actor_id=uuid4(), actor_telegram_id=123,
        attempt_id=attempt, reason="  Receipt unreadable  ",
    )

    assert result == attempt
    assert rpc.await_args.kwargs["arguments"]["reason"] == "Receipt unreadable"
