"""Operator's bank-review decision must not end in an orphan Telegram screen."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules import platform_dashboard_handlers as ui


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
        if h.callback.__name__ == "platform_bank_receipt_reference"
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
@pytest.mark.parametrize("reason,decision", [
    ("Test bank funds not received", "no"),
    ("Verified bank reference 12345", "ok"),
    ("/cancel", "no"),
])
async def test_review_result_navigation_and_rich_card(monkeypatch, reason, decision):
    attempt = uuid4()
    query = _query(attempt, decision)
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        ui, "saas_v2_pending_proofs",
        AsyncMock(return_value={"items": [{"id": str(attempt)}]}),
    )
    approve = AsyncMock(return_value=uuid4())
    reject = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(ui, "saas_v2_confirm_attempt", approve)
    monkeypatch.setattr(ui, "saas_v2_reject_attempt", reject)
    rich = AsyncMock()
    monkeypatch.setattr(ui, "send_owner_rich", rich)

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    callback, entry = _setup()
    await callback(query, bot)
    message = SimpleNamespace(from_user=query.from_user, text=reason)
    await entry(message, bot)
    rich.assert_awaited_once()
    kwargs = rich.await_args.kwargs
    assert kwargs["chat_id"] == 7777
    buttons = [
        button.callback_data for row in
        kwargs["reply_markup"].inline_keyboard for button in row
    ]
    assert buttons == ["plat:v2proofs:0", "plat:billing:0"]
    assert "<h3>" in kwargs["rich_html"]
    assert "<table bordered compact>" in kwargs["rich_html"]
    if reason == "/cancel":
        approve.assert_not_awaited()
        reject.assert_not_awaited()
        assert "Проверка отменена" in kwargs["rich_html"]
    elif decision == "ok":
        approve.assert_awaited_once()
        reject.assert_not_awaited()
        assert "Оплата подтверждена" in kwargs["rich_html"]
    else:
        reject.assert_awaited_once()
        approve.assert_not_awaited()
        assert "Чек отклонён" in kwargs["rich_html"]
        assert reason in kwargs["rich_html"]
        assert "неоплаченным" in kwargs["classic_html"]
