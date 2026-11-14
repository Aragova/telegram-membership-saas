"""Live payment-review results without real Telegram or bank operations."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules import platform_saas_review_results as notice


def _item(*, decision="rejected", bot=900, chat=700):
    return {
        "id": str(uuid4()), "decision": decision, "generation": 1,
        "source_bot": bot, "source_chat": chat,
        "source_project": str(uuid4()),
        "amount": "150.00", "currency": "RUB",
        "reason": "Bank money not received <check>",
    }


def test_rejection_reason_is_readable_and_escaped(monkeypatch):
    monkeypatch.setattr(notice, "tr", lambda ru, en: ru)
    rich, classic = notice._texts(_item())
    assert "Чек отклонён" in rich
    assert "150.00 RUB" in rich
    assert "Bank money not received &lt;check&gt;" in rich
    assert "Счёт остаётся неоплаченным" in classic
    assert "<check>" not in rich
    assert "3293dab1" not in classic


def test_confirmed_result_is_bilingual_without_rejection_reason(monkeypatch):
    monkeypatch.setattr(notice, "tr", lambda ru, en: en)
    rich, classic = notice._texts(_item(decision="approved"))
    assert "Payment confirmed" in rich
    assert "Verified bank payment" in rich
    assert "Reason" not in rich
    assert "Bank money not received" not in classic


@pytest.mark.asyncio
async def test_result_uses_same_original_bot_and_closes_token_session(monkeypatch):
    item = _item()
    original = SimpleNamespace(
        id=item["source_bot"],
        session=SimpleNamespace(close=AsyncMock()),
    )
    central = SimpleNamespace(id=111)
    lookups = []
    class Session:
        async def scalar(self, query):
            lookups.append(query)
            return "encrypted-token"

    @asynccontextmanager
    async def source_scope(factory, project_id):
        assert str(project_id) == item["source_project"]
        yield Session()

    @asynccontextmanager
    async def language(factory, chat_id):
        assert chat_id == item["source_chat"]
        yield

    created = []
    def bot_factory(*, token):
        created.append(token)
        return original

    monkeypatch.setattr(notice, "system_project_session_scope", source_scope)
    monkeypatch.setattr(notice, "recipient_language", language)
    monkeypatch.setattr(notice, "decrypt_token",
                        lambda cipher, *, key: "decrypted")
    monkeypatch.setattr(notice, "Bot", bot_factory)
    send = AsyncMock()
    monkeypatch.setattr(notice, "_send_result", send)

    await notice._send_using_source_bot(
        object(), central_bot=central,
        settings=SimpleNamespace(bot_token_encryption_key="secret"),
        item=item,
    )
    send.assert_awaited_once_with(original, item["source_chat"], item)
    original.session.close.assert_awaited_once()
    assert created == ["decrypted"]
    assert len(lookups) == 1


@pytest.mark.asyncio
async def test_result_from_central_bot_reuses_its_live_transport(monkeypatch):
    item = _item(bot=111)
    central = SimpleNamespace(id=111)
    @asynccontextmanager
    async def language(factory, chat_id):
        yield
    monkeypatch.setattr(notice, "recipient_language", language)
    source_scope = AsyncMock()
    monkeypatch.setattr(notice, "system_project_session_scope", source_scope)
    sender = AsyncMock()
    monkeypatch.setattr(notice, "_send_result", sender)

    await notice._send_using_source_bot(
        object(), central_bot=central,
        settings=SimpleNamespace(bot_token_encryption_key=None), item=item,
    )
    sender.assert_awaited_once_with(central, item["source_chat"], item)
    source_scope.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_durable_result_fenced_finish_without_bank_replay(monkeypatch, failed):
    item = _item()
    claim = AsyncMock(return_value=item)
    finish = AsyncMock()
    send = AsyncMock()
    if failed:
        send.side_effect = RuntimeError("Telegram unreachable")
    monkeypatch.setattr(notice, "_claim", claim)
    monkeypatch.setattr(notice, "_finish", finish)
    monkeypatch.setattr(notice, "_send_using_source_bot", send)
    actor = uuid4()
    assert await notice.deliver_one_saas_review_result(
        object(), central_bot=SimpleNamespace(id=77),
        settings=SimpleNamespace(), actor_id=actor,
    )
    finish.assert_awaited_once()
    assert finish.await_args.kwargs["actor_id"] == actor
    assert str(finish.await_args.kwargs["attempt_id"]) == item["id"]
    assert finish.await_args.kwargs["generation"] == 1
    assert finish.await_args.kwargs["sent"] is not failed


@pytest.mark.asyncio
async def test_no_decision_does_not_send(monkeypatch):
    monkeypatch.setattr(notice, "_claim", AsyncMock(return_value=None))
    send = AsyncMock()
    monkeypatch.setattr(notice, "_send_using_source_bot", send)
    assert not await notice.deliver_one_saas_review_result(
        object(), central_bot=SimpleNamespace(id=77),
        settings=SimpleNamespace(), actor_id=uuid4(),
    )
    send.assert_not_awaited()
