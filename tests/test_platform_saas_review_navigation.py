"""One-tap operator approval, rejected proof reason and familiar navigation.

Never touches real bank accounts, Telegram, or the financial database.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules import platform_saas_v2_payment_service as billing


def _setup():
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=Settings(
            platform_owner_telegram_id=7777,
            platform_timezone="Europe/Moscow",
        ),
    )
    callback = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "platform_callback"
    )
    entry = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "platform_bank_receipt_rejection_reason"
    )
    return callback, entry


def _query(attempt, decision):
    user = SimpleNamespace(id=7777, username="operator", first_name="Admin")
    return SimpleNamespace(
        data=f"plat:v2review:{decision}:{ui.encode_uuid(attempt)}:0",
        from_user=user,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=7777, type="private"),
            message_id=42,
        ),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_one_tap_approves_without_bank_reference_input(monkeypatch):
    attempt = uuid4()
    query = _query(attempt, "ok")
    actor = uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(
        ui, "saas_v2_pending_proofs",
        AsyncMock(return_value={"items": [{"id": str(attempt)}]}),
    )
    approve = AsyncMock(return_value=uuid4())
    reject = AsyncMock()
    monkeypatch.setattr(ui, "saas_v2_confirm_attempt_one_tap", approve)
    monkeypatch.setattr(ui, "saas_v2_reject_attempt", reject)
    rich = AsyncMock()
    monkeypatch.setattr(ui, "send_owner_rich", rich)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    callback, entry = _setup()

    # Approval is committed directly by the callback, no form or next step.
    await callback(query, bot)
    approve.assert_awaited_once()
    assert approve.await_args.kwargs["attempt_id"] == attempt
    assert approve.await_args.kwargs["actor_id"] == actor
    reject.assert_not_awaited()
    rich.assert_awaited_once()
    assert "Оплата подтверждена" in rich.await_args.kwargs["rich_html"]
    buttons = [
        b.callback_data for row in
        rich.await_args.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert buttons == ["plat:v2proofs:0", "plat:billing:0"]
    assert "<h3>" in rich.await_args.kwargs["rich_html"]
    assert "<table bordered compact>" in rich.await_args.kwargs["rich_html"]
    # No leftover editor: a regular text after Approve cannot make another
    # ledger allocation.
    filter_obj = next(
        h.filters[0].callback for h in
        ui.create_platform_dashboard_router(
            MagicMock(), settings=Settings(platform_owner_telegram_id=7777),
        ).message.handlers
        if h.callback.__name__ == "platform_bank_receipt_rejection_reason"
    )
    message = SimpleNamespace(
        from_user=query.from_user,
        chat=SimpleNamespace(type="private"),
        text="BANK123",
    )
    assert not await filter_obj(message)
    approve.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Payment not received", "/cancel"])
async def test_rejection_still_collects_reason_and_returns(monkeypatch, reason):
    attempt = uuid4()
    query = _query(attempt, "no")
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        ui, "saas_v2_pending_proofs",
        AsyncMock(return_value={"items": [{"id": str(attempt)}]}),
    )
    approve = AsyncMock()
    reject = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(ui, "saas_v2_confirm_attempt_one_tap", approve)
    monkeypatch.setattr(ui, "saas_v2_reject_attempt", reject)
    rich = AsyncMock()
    monkeypatch.setattr(ui, "send_owner_rich", rich)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    callback, entry = _setup()

    await callback(query, bot)
    approve.assert_not_awaited()
    reject.assert_not_awaited()
    await entry(SimpleNamespace(from_user=query.from_user, text=reason), bot)
    rich.assert_awaited_once()
    result = rich.await_args.kwargs["rich_html"]
    if reason == "/cancel":
        reject.assert_not_awaited()
        assert "Проверка отменена" in result
    else:
        reject.assert_awaited_once()
        assert reject.await_args.kwargs["reason"] == reason
        assert "Чек отклонён" in result
    buttons = [
        b.callback_data for row in
        rich.await_args.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert buttons == ["plat:v2proofs:0", "plat:billing:0"]


@pytest.mark.asyncio
async def test_stale_proof_cannot_be_approved_from_old_keyboard(monkeypatch):
    attempt = uuid4()
    query = _query(attempt, "ok")
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        ui, "saas_v2_pending_proofs",
        AsyncMock(return_value={"items": []}),
    )
    approve = AsyncMock()
    monkeypatch.setattr(ui, "saas_v2_confirm_attempt_one_tap", approve)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    callback, _ = _setup()
    await callback(query, bot)
    approve.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True


def test_generated_operator_reference_is_not_fake_bank_transaction_number():
    attempt = uuid4()
    reference = billing.operator_approval_reference(attempt)
    assert reference == "VIMI-OPERATOR-" + attempt.hex
    assert 8 <= len(reference) <= 160
    assert reference != billing.operator_approval_reference(uuid4())
    assert reference == billing.operator_approval_reference(attempt)


@pytest.mark.asyncio
async def test_one_tap_backend_reuses_existing_sql_approval_with_stable_reference(
    monkeypatch,
):
    attempt = uuid4()
    actor = uuid4()
    approved = uuid4()
    confirmed = AsyncMock(return_value=approved)
    monkeypatch.setattr(billing, "saas_v2_confirm_attempt", confirmed)
    settings = Settings(platform_owner_telegram_id=7777)
    factory = object()
    result = await billing.saas_v2_confirm_attempt_one_tap(
        factory, actor_id=actor, actor_telegram_id=7777,
        attempt_id=attempt, settings=settings,
    )
    assert result == approved
    confirmed.assert_awaited_once_with(
        factory,
        actor_id=actor,
        actor_telegram_id=7777,
        attempt_id=attempt,
        verified_bank_reference=billing.operator_approval_reference(attempt),
        settings=settings,
    )
