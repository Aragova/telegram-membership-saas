"""Telegram owner can remove a BotInstance safely without touching SQL."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _callback(action: str):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(
            id=12345678, first_name="Owner", username="owner",
        ),
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )


def _ui(bot_id, project_id, *, owner=True, status="registered"):
    return SetupOverview(
        project_id=project_id, project_name="Paid channel",
        timezone="Europe/Moscow",
        bots=(("my_buyer_bot", status),),
        resources=(), plans=(), methods=(),
        bot_instances=((bot_id, "my_buyer_bot", status),),
        can_delete_bot=owner,
    )


def _handler():
    router = ui.create_owner_setup_router(MagicMock())
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


async def test_owner_bot_page_has_confirmed_deletion_and_from_zero_link(monkeypatch):
    owner_id, project_id, instance_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=owner_id)
    )
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=project_id)
    )
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(return_value=_ui(instance_id, project_id)),
    )
    bot = _bot()
    await _handler()(_callback("bots"), bot)
    sent = bot.send_message.await_args
    callbacks = [
        b.callback_data
        for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row if b.callback_data is not None
    ]
    delete = "own:drop:" + encode_uuid(project_id) + ":" + encode_uuid(instance_id)
    assert delete in callbacks
    assert "own:new" in callbacks
    assert all(len(x.encode()) <= 64 for x in callbacks)

    await _handler()(_callback(delete.removeprefix("own:")), bot)
    confirmation = bot.send_message.await_args
    assert "Удалить @my_buyer_bot" in confirmation.args[1]
    assert "история платежей" in confirmation.args[1]
    callbacks = [
        b.callback_data
        for row in confirmation.kwargs["reply_markup"].inline_keyboard
        for b in row if b.callback_data is not None
    ]
    assert ("own:dropok:" + encode_uuid(project_id) + ":"
            + encode_uuid(instance_id)) in callbacks


async def test_no_delete_button_for_nonowner_or_currently_deleting(monkeypatch):
    project_id, instance_id = uuid4(), uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4())
    )
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=project_id)
    )
    bot = _bot()
    snapshot = AsyncMock(return_value=_ui(instance_id, project_id, owner=False))
    monkeypatch.setattr(ui, "setup_overview", snapshot)
    await _handler()(_callback("bots"), bot)
    rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert all("Удалить" not in b.text for row in rows for b in row)
    snapshot.return_value = _ui(instance_id, project_id, status="deleting")
    await _handler()(_callback("bots"), bot)
    assert "отключается" in bot.send_message.await_args.args[1]
    rows = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert all("Удалить" not in b.text for row in rows for b in row)


async def test_stale_delete_button_from_other_project_cannot_remove(monkeypatch):
    selected, old, instance_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4())
    )
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=selected)
    )
    deletion = AsyncMock()
    monkeypatch.setattr(ui, "request_bot_retirement", deletion)
    bot = _bot()
    query = _callback(
        "dropok:" + encode_uuid(old) + ":" + encode_uuid(instance_id)
    )
    await _handler()(query, bot)
    deletion.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_owner_delete_confirmation_triggers_retirement_then_refresh(monkeypatch):
    selected, instance_id = uuid4(), uuid4()
    owner = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=selected)
    )
    deleting = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "request_bot_retirement", deleting)
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(return_value=_ui(instance_id, selected, status="deleting")),
    )
    bot = _bot()
    query = _callback(
        "dropok:" + encode_uuid(selected) + ":" + encode_uuid(instance_id)
    )
    await _handler()(query, bot)
    deleting.assert_awaited_once()
    assert deleting.await_args.kwargs["project_id"] == selected
    assert deleting.await_args.kwargs["bot_instance_id"] == instance_id
    assert bot.send_message.await_count == 2
    query.answer.assert_awaited_once()
