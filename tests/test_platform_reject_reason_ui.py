"""Production regression: a 4-character test rejection is not a bank error."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _user():
    return SimpleNamespace(id=7777, username="owner", first_name="Operator")


def _review_ui():
    router = ui.create_platform_dashboard_router(
        MagicMock(), settings=Settings(platform_owner_telegram_id=7777),
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


@pytest.mark.asyncio
async def test_four_letter_reason_is_explained_before_database_reject(monkeypatch):
    attempt = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(ui, "saas_v2_pending_proofs", AsyncMock(
        return_value={"items": [{"id": str(attempt)}]},
    ))
    reject = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(ui, "saas_v2_reject_attempt", reject)
    monkeypatch.setattr(ui, "_render_platform_screen", AsyncMock())
    button, entry = _review_ui()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    query = SimpleNamespace(
        from_user=_user(), data="plat:v2review:no:" + encode_uuid(attempt) + ":0",
        message=SimpleNamespace(
            message_id=101,
            chat=SimpleNamespace(type="private", id=7777),
        ),
        answer=AsyncMock(),
    )
    await button(query, bot)
    query.answer.assert_awaited_once()
    message = SimpleNamespace(from_user=_user(), text="тест")
    await entry(message, bot)
    bot.send_message.assert_awaited_once()
    assert "5 до 500 символов" in bot.send_message.await_args.args[1]
    assert "Сам счёт не удаляется" in bot.send_message.await_args.args[1]
    reject.assert_not_awaited()

    bot.send_message.reset_mock()
    message.text = "Тестовый чек"
    await entry(message, bot)
    reject.assert_awaited_once()
    assert reject.await_args.kwargs["attempt_id"] == attempt
    assert reject.await_args.kwargs["reason"] == "Тестовый чек"
    bot.send_message.assert_awaited_once()
    assert "Заявка отклонена" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_english_invalid_reason_reports_constraint_without_sql(monkeypatch):
    attempt = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(ui, "saas_v2_pending_proofs", AsyncMock(
        return_value={"items": [{"id": str(attempt)}]},
    ))
    reject = AsyncMock()
    monkeypatch.setattr(ui, "saas_v2_reject_attempt", reject)
    monkeypatch.setattr(ui, "_render_platform_screen", AsyncMock())
    button, entry = _review_ui()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    query = SimpleNamespace(
        from_user=_user(), data="plat:v2review:no:" + encode_uuid(attempt) + ":0",
        message=SimpleNamespace(
            message_id=101,
            chat=SimpleNamespace(type="private", id=7777),
        ),
        answer=AsyncMock(),
    )
    token = _language.set("en")
    try:
        await button(query, bot)
        await entry(SimpleNamespace(from_user=_user(), text="test"), bot)
        assert "5–500 characters" in bot.send_message.await_args.args[1]
        reject.assert_not_awaited()
    finally:
        _language.reset(token)
