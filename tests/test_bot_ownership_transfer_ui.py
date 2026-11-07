"""No transfer without two real account confirmations and token proof."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from test_owner_subscribers_ui import _bot, _buttons, _callbacks
from test_owner_subscribers_ui import _query as q

from telegram_membership_saas.modules.owner_setup import transfer_handlers as ui
from telegram_membership_saas.modules.owner_setup.transfers import TransferView
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _view(project, owner, recipient, *, status="pending"):
    return TransferView(
        id=uuid4(), project_id=project, bot_id=77889999,
        bot_username="test_transfer_bot", from_user_id=owner,
        to_user_id=recipient, status=status,
        expires_at=datetime.now(UTC) + timedelta(hours=5),
    )


def _query(action):
    return SimpleNamespace(
        data=action,
        message=SimpleNamespace(chat=SimpleNamespace(id=123, type="private")),
        from_user=SimpleNamespace(id=123, username="sender", first_name="Sender"),
        answer=AsyncMock(),
    )


def _cb(router):
    return next(h.callback for h in router.callback_query.handlers
                if h.callback.__name__ == "transfer_callback")


async def test_recipient_accept_requires_token_and_old_owner_final_confirmation(monkeypatch):
    project, owner, recipient = uuid4(), uuid4(), uuid4()
    item = _view(project, owner, recipient)
    router = ui.create_transfer_router(MagicMock(), project_id=project)
    bot = _bot()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=recipient))
    monkeypatch.setattr(ui, "get_transfer_invite", AsyncMock(return_value=item))
    accept = AsyncMock()
    finalize = AsyncMock()
    monkeypatch.setattr(ui, "accept_transfer_invite", accept)
    monkeypatch.setattr(ui, "finalize_transfer", finalize)
    await _cb(router)(_query("xfr:accept:" + encode_uuid(item.id)), bot)
    accept.assert_not_awaited()
    finalize.assert_not_awaited()
    assert "НОВЫЙ токен" in bot.send_message.await_args.args[1]

    entry = next(h.callback for h in router.message.handlers
                 if h.callback.__name__ == "transfer_input")
    token_message = SimpleNamespace(
        text="12345678:" + "A" * 35,
        message_id=456, chat=SimpleNamespace(id=123, type="private"),
        from_user=SimpleNamespace(id=123, username="sender", first_name="Sender"),
    )
    bot.delete_message = AsyncMock(return_value=True)
    accept.return_value = _view(project, owner, recipient, status="accepted")
    await entry(token_message, bot)
    bot.delete_message.assert_awaited_once_with(123, 456)
    assert accept.await_args.kwargs["actor_id"] == recipient
    assert accept.await_args.kwargs["transfer_id"] == item.id

    # A recipient can never finalize the old owner's bot via a crafted callback.
    monkeypatch.setattr(ui, "get_transfer_invite", AsyncMock(return_value=item))
    await _cb(router)(_query("xfr:finalok:" + encode_uuid(item.id)), bot)
    finalize.assert_not_awaited()


async def test_transfer_token_not_used_when_telegram_cannot_delete_message(monkeypatch):
    project, owner, recipient = uuid4(), uuid4(), uuid4()
    item = _view(project, owner, recipient)
    router = ui.create_transfer_router(MagicMock(), project_id=project)
    bot = _bot()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=recipient))
    monkeypatch.setattr(ui, "get_transfer_invite", AsyncMock(return_value=item))
    accept = AsyncMock()
    monkeypatch.setattr(ui, "accept_transfer_invite", accept)
    await _cb(router)(_query("xfr:accept:" + encode_uuid(item.id)), bot)
    input_fn = next(h.callback for h in router.message.handlers
                    if h.callback.__name__ == "transfer_input")
    bot.delete_message = AsyncMock(return_value=False)
    token_message = SimpleNamespace(
        text="12345678:" + "A" * 35,
        message_id=456, chat=SimpleNamespace(id=123, type="private"),
        from_user=SimpleNamespace(id=123, username="sender", first_name="Sender"),
    )
    await input_fn(token_message, bot)
    accept.assert_not_awaited()
    assert "Токен не удалось удалить" in bot.send_message.await_args.args[1]


async def test_owner_transfer_control_only_in_connected_settings(monkeypatch):
    project = uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    bot = _bot()
    await cb(q("settings"), bot)
    assert "own:transfer" in _buttons(bot)
