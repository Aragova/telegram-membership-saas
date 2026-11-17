"""Recovery from provider-side Telegram Stars history, not fragile updates."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_membership_saas.modules import platform_saas_stars_recovery as recovery


def tx(*, charge="central-charge-123456", amount=60, payload="vps:test", payer=123):
    return SimpleNamespace(
        id=charge, amount=amount, nanostar_amount=0,
        date=datetime.now(UTC),
        source=SimpleNamespace(
            type="user", transaction_type="invoice_payment",
            user=SimpleNamespace(id=payer), invoice_payload=payload,
        ),
        receiver=None,
    )


async def test_provider_history_recovers_missing_success_payment(monkeypatch):
    confirm = AsyncMock(return_value={"applied": True, "duplicate": False})
    monkeypatch.setattr(recovery, "confirm_platform_stars_charge", confirm)
    assert await recovery.reconcile_provider_stars_transaction(
        MagicMock(), bot_id=9876, transaction=tx(),
    )
    kwargs = confirm.await_args.kwargs
    assert kwargs["payer_telegram_id"] == 123
    assert kwargs["central_bot_id"] == 9876
    assert kwargs["charge_id"] == "central-charge-123456"
    assert kwargs["stars_amount"] == 60
    assert kwargs["currency"] == "XTR"


async def test_provider_history_is_exactly_once_on_replayed_update(monkeypatch):
    confirm = AsyncMock(return_value={"applied": True, "duplicate": True})
    monkeypatch.setattr(recovery, "confirm_platform_stars_charge", confirm)
    assert not await recovery.reconcile_provider_stars_transaction(
        MagicMock(), bot_id=9876, transaction=tx(),
    )
    confirm.assert_awaited_once()


@pytest.mark.parametrize("mutate", [
    {"payload": "tenant:order"},
    {"payload": None},
    {"amount": 0},
    {"amount": -60},
])
async def test_provider_does_not_guess_unmatched_invoice(monkeypatch, mutate):
    confirm = AsyncMock()
    monkeypatch.setattr(recovery, "confirm_platform_stars_charge", confirm)
    item = tx(payload=mutate.get("payload", "vps:test"),
              amount=mutate.get("amount", 60))
    assert not await recovery.reconcile_provider_stars_transaction(
        MagicMock(), bot_id=9876, transaction=item,
    )
    confirm.assert_not_awaited()


async def test_provider_history_recovers_refund_with_optional_payload_missing(
    monkeypatch,
):
    lookup = AsyncMock(return_value="vps:original-order")
    refund = AsyncMock(return_value=True)
    monkeypatch.setattr(recovery, "recover_platform_stars_charge_payload", lookup)
    monkeypatch.setattr(recovery, "record_platform_stars_refund", refund)
    item = tx(charge="refund-123456789", amount=-60)
    item.source = None
    item.receiver = SimpleNamespace(
        type="user", transaction_type="invoice_payment",
        user=SimpleNamespace(id=123), invoice_payload=None,
    )
    assert await recovery.reconcile_provider_stars_transaction(
        MagicMock(), bot_id=9876, transaction=item,
    )
    assert lookup.await_args.kwargs["charge_id"] == "refund-123456789"
    kwargs = refund.await_args.kwargs
    assert kwargs["payload"] == "vps:original-order"
    assert kwargs["stars_amount"] == 60
    assert kwargs["charge_id"] == "refund-123456789"


async def test_refund_without_known_original_charge_never_guesses(monkeypatch):
    lookup = AsyncMock(return_value=None)
    refund = AsyncMock()
    monkeypatch.setattr(recovery, "recover_platform_stars_charge_payload", lookup)
    monkeypatch.setattr(recovery, "record_platform_stars_refund", refund)
    item = tx(amount=-60)
    item.source = None
    item.receiver = SimpleNamespace(
        type="user", transaction_type="invoice_payment",
        user=SimpleNamespace(id=123), invoice_payload=None,
    )
    assert not await recovery.reconcile_provider_stars_transaction(
        MagicMock(), bot_id=9876, transaction=item,
    )
    refund.assert_not_awaited()


async def test_provider_page_failure_never_advances_pagination(monkeypatch):
    confirm = AsyncMock(side_effect=ConnectionError("PostgreSQL disconnected"))
    monkeypatch.setattr(recovery, "confirm_platform_stars_charge", confirm)
    bot = MagicMock()
    bot.id = 9876
    bot.get_star_transactions = AsyncMock(
        return_value=SimpleNamespace(transactions=[tx()]),
    )
    with pytest.raises(ConnectionError):
        await recovery.reconcile_platform_stars_page(
            MagicMock(), bot=bot, offset=200,
        )
    assert bot.get_star_transactions.await_args.kwargs == {
        "offset": 200, "limit": 100,
    }


async def test_provider_page_returns_cursor_only_after_complete_processing(monkeypatch):
    confirm = AsyncMock(return_value={"applied": True, "duplicate": False})
    monkeypatch.setattr(recovery, "confirm_platform_stars_charge", confirm)
    bot = MagicMock()
    bot.id = 9876
    bot.get_star_transactions = AsyncMock(
        return_value=SimpleNamespace(transactions=[tx()]),
    )
    assert await recovery.reconcile_platform_stars_page(
        MagicMock(), bot=bot, offset=200,
    ) == (201, True)
    confirm.assert_awaited_once()


async def test_retry_keeps_offset_after_transient_failure(monkeypatch):
    calls = []
    confirm = AsyncMock(return_value={"applied": True, "duplicate": False})
    monkeypatch.setattr(recovery, "confirm_platform_stars_charge", confirm)
    bot = MagicMock()
    bot.id = 9876
    bot.get_star_transactions = AsyncMock(side_effect=[
        ConnectionError("provider history unavailable"),
        SimpleNamespace(transactions=[tx()]),
    ])

    async def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise asyncio.CancelledError()

    import asyncio

    monkeypatch.setattr(recovery.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await recovery.run_platform_stars_recovery_loop(
            MagicMock(), bot=bot,
        )
    offsets = [x.kwargs["offset"] for x in bot.get_star_transactions.await_args_list]
    assert offsets == [0, 0]
    assert calls == [recovery.RETRY_DELAY_SECONDS, recovery.FULL_SCAN_DELAY_SECONDS]
    confirm.assert_awaited_once()
