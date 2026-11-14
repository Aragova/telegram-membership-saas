"""Central proof alerts: one actionable message, never a duplicate menu."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules import platform_saas_proof_alerts as alerts


@asynccontextmanager
async def _language(*_args):
    yield


def _item():
    return {
        "id": str(uuid4()), "kind": "new",
        "generation": 1, "amount": "150.00",
        "currency": "RUB", "pending_count": 2,
    }


@pytest.mark.asyncio
async def test_central_alert_is_regular_private_message_with_existing_queue(
    monkeypatch,
):
    item = _item()
    claim = AsyncMock(return_value=item)
    finish = AsyncMock()
    monkeypatch.setattr(alerts, "_claim", claim)
    monkeypatch.setattr(alerts, "_finish", finish)
    monkeypatch.setattr(alerts, "recipient_language", _language)
    bot = SimpleNamespace(send_message=AsyncMock())
    actor = uuid4()

    worked = await alerts.deliver_one_platform_proof_alert(
        object(), bot=bot, platform_actor_id=actor, owner_telegram_id=777,
    )
    assert worked
    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.await_args
    assert args[0] == 777
    assert "150.00 RUB" in args[1]
    assert "2" in args[1]
    assert str(item["id"]) not in args[1]  # No technical UUID in phone push.
    assert kwargs["parse_mode"] == "HTML"
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert len(keyboard) == 1
    assert len(keyboard[0]) == 1
    assert keyboard[0][0].callback_data == "plat:v2proofs:0"
    finish.assert_awaited_once()
    assert finish.await_args.kwargs["platform_actor_id"] == actor
    assert str(finish.await_args.kwargs["attempt_id"]) == item["id"]
    assert finish.await_args.kwargs["kind"] == "new"
    assert finish.await_args.kwargs["generation"] == 1
    assert finish.await_args.kwargs["sent"] is True


@pytest.mark.asyncio
async def test_absent_proof_never_sends_phone_message(monkeypatch):
    monkeypatch.setattr(alerts, "_claim", AsyncMock(return_value=None))
    finish = AsyncMock()
    monkeypatch.setattr(alerts, "_finish", finish)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert not await alerts.deliver_one_platform_proof_alert(
        object(), bot=bot,
        platform_actor_id=uuid4(), owner_telegram_id=777,
    )
    bot.send_message.assert_not_awaited()
    finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_failure_requeues_without_fake_success(monkeypatch):
    item = _item()
    finish = AsyncMock()
    monkeypatch.setattr(alerts, "_claim", AsyncMock(return_value=item))
    monkeypatch.setattr(alerts, "_finish", finish)
    monkeypatch.setattr(alerts, "recipient_language", _language)
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=RuntimeError("offline")),
    )
    assert await alerts.deliver_one_platform_proof_alert(
        object(), bot=bot,
        platform_actor_id=uuid4(), owner_telegram_id=777,
    )
    bot.send_message.assert_awaited_once()
    assert finish.await_args.kwargs["sent"] is False


def test_alert_text_russian_and_english(monkeypatch):
    monkeypatch.setattr(alerts, "tr", lambda ru, en: ru)
    assert "Новый чек на проверку" in alerts._message(_item())
    monkeypatch.setattr(alerts, "tr", lambda ru, en: en)
    assert "New receipt for review" in alerts._message(_item())


@pytest.mark.parametrize("kind,label", [
    ("new","Новый чек на проверку"),
    ("six_hour","Чек ожидает 6 часов"),
    ("eleven_hour","Остался 1 час"),
])
def test_reminder_text_uses_same_review_queue(monkeypatch,kind,label):
    monkeypatch.setattr(alerts,"tr",lambda ru,en: ru)
    data=_item()
    data["kind"]=kind
    assert label in alerts._message(data)
