"""Native Bot API refunded_payment is the only file Stars refund ingress."""
from datetime import UTC, datetime
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from aiogram.types import Message

from telegram_membership_saas.modules.file_sales import handlers as ui


def _handler(router, name):
    return next(
        entry.callback for entry in router.message.handlers
        if entry.callback.__name__ == name
    )


async def test_refunded_payment_from_telegram_records_a_single_source_event(
    monkeypatch,
):
    project = uuid4()
    router = ui.create_file_sales_router(MagicMock(), project_id=project)
    refund = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(ui, "record_telegram_file_star_refund", refund)
    bot = MagicMock()
    bot.id = 9999
    payload = "fs:" + uuid4().hex
    # Construct the actual aiogram Bot API service message. Telegram sends
    # refunded_payment without requiring buyer-authored messages or receipts.
    message = Message.model_validate({
        "message_id": 5,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": 456, "type": "private"},
        "refunded_payment": {
            "currency": "XTR", "total_amount": 150,
            "invoice_payload": payload,
            "telegram_payment_charge_id": "original_charge_1",
        },
    })
    assert message.refunded_payment is not None
    assert message.date.tzinfo is not None
    await _handler(router, "file_refund_notice")(message, bot)
    refund.assert_awaited_once()
    assert refund.await_args.kwargs == {
        "project_id": project,
        "bot_id": 9999,
        "buyer_id": 456,
        "invoice_payload": payload,
        "amount": 150,
        "currency": "XTR",
        "charge_id": "original_charge_1",
        "refunded_at": message.date,
    }


async def test_refund_service_message_not_swallowed_by_active_file_editor():
    states = {456: {"stage": "upload", "expires": monotonic() + 300}}
    matcher = ui.FileEditorFilter(states)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=456),
        chat=SimpleNamespace(id=456, type="private"),
        text=None, successful_payment=None,
        refunded_payment=SimpleNamespace(
            invoice_payload="fs:verified",
        ),
    )
    assert not await matcher(message)
    assert 456 in states


async def test_invalid_refund_update_cannot_cancel_buyer_delivery(monkeypatch):
    router = ui.create_file_sales_router(MagicMock(), project_id=uuid4())
    refund = AsyncMock(side_effect=PermissionError("Refund mismatch"))
    monkeypatch.setattr(ui, "record_telegram_file_star_refund", refund)
    bot = MagicMock()
    bot.id = 9999
    event = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=456),
        refunded_payment=SimpleNamespace(
            invoice_payload="fs:" + uuid4().hex,
            currency="XTR", total_amount=123,
            telegram_payment_charge_id="other_charge",
        ),
        date=datetime.now(UTC),
    )
    # The record is rejected and logged, but existing purchases are not
    # mutated and Telegram's polling handler remains responsive.
    await _handler(router, "file_refund_notice")(event, bot)
    refund.assert_awaited_once()
    bot.send_message.assert_not_called()
