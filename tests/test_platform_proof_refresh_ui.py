"""Bank receipt queue Refresh genuinely re-queries and acknowledges the operator.

No external Telegram calls, billing decisions, fake payment records or new UI.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from telegram_membership_saas.config import Settings
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_dashboard_handlers as ui


def _query():
    user = SimpleNamespace(
        id=7777, username="platform", first_name="Operator",
    )
    return SimpleNamespace(
        data="plat:v2proofs:0",
        from_user=user,
        message=SimpleNamespace(
            message_id=421,
            chat=SimpleNamespace(id=user.id, type="private"),
        ),
        answer=AsyncMock(),
    )


def _handler(factory):
    router = ui.create_platform_dashboard_router(
        factory,
        settings=Settings(
            platform_owner_telegram_id=7777,
            platform_timezone="Europe/Moscow",
        ),
    )
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "platform_callback"
    )


@pytest.mark.asyncio
async def test_refresh_reads_new_pending_items_and_updates_same_screen(monkeypatch):
    factory = MagicMock()
    check = AsyncMock(side_effect=[
        {"items": []},
        {"items": [{
            "id": str(uuid4()), "invoice_id": str(uuid4()),
            "amount": "150.00", "currency": "RUB", "rub": "150.00",
            "source_bot": 9096954115, "source_chat": 8593748427,
            "source_message": 242,
        }]},
    ])
    monkeypatch.setattr(ui, "saas_v2_pending_proofs", check)
    monkeypatch.setattr(
        ui, "ensure_telegram_actor",
        AsyncMock(return_value=uuid4()),
    )
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    callback = _handler(factory)

    first = _query()
    await callback(first, bot)
    body0 = bot.edit_message_text.await_args.args[0]
    assert "Новых заявок нет." in body0
    assert "⏱ Проверено: " in body0
    first.answer.assert_awaited_once_with("✅ Список проверен")

    second = _query()
    await callback(second, bot)
    body1 = bot.edit_message_text.await_args.args[0]
    assert "Новых заявок нет." not in body1
    assert "150.00 RUB" in body1
    assert "⏱ Проверено: " in body1
    assert bot.edit_message_text.await_count == 2
    assert all(
        call.kwargs["message_id"] == 421
        for call in bot.edit_message_text.await_args_list
    )
    bot.send_message.assert_not_awaited()
    assert check.await_count == 2
    second.answer.assert_awaited_once_with("✅ Список проверен")
    keyboard = bot.edit_message_text.await_args.kwargs["reply_markup"]
    actions = [
        btn.callback_data for row in keyboard.inline_keyboard for btn in row
    ]
    assert "plat:v2proofs:0" in actions
    assert "plat:billing:0" in actions
    assert any(a.startswith("plat:v2review:no:") for a in actions)


@pytest.mark.asyncio
async def test_unchanged_queue_refresh_uses_quiet_toast_not_new_message(monkeypatch):
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    fresh_read = AsyncMock(return_value={"items": []})
    monkeypatch.setattr(ui, "saas_v2_pending_proofs", fresh_read)
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(
        method=EditMessageText(
            chat_id=7777, message_id=421, text="same list",
        ),
        message="Bad Request: message is not modified",
    ))
    bot.send_message = AsyncMock()
    handler = _handler(MagicMock())
    token = _language.set("en")
    try:
        for _ in range(2):
            q = _query()
            await handler(q, bot)
            q.answer.assert_awaited_once_with("✅ List checked")
        assert bot.edit_message_text.await_count == 2
        assert fresh_read.await_count == 2
        text = bot.edit_message_text.await_args.args[0]
        assert "⏱ Checked: " in text
        bot.send_message.assert_not_awaited()
    finally:
        _language.reset(token)


@pytest.mark.asyncio
async def test_invalid_refresh_page_never_runs_database_query(monkeypatch):
    query = _query()
    query.data = "plat:v2proofs:-1"
    read = AsyncMock()
    monkeypatch.setattr(ui, "saas_v2_pending_proofs", read)
    bot = MagicMock()
    await _handler(MagicMock())(query, bot)
    read.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True
