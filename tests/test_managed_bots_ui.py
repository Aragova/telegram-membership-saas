"""Native Telegram managed-bot creation is opt-in, private, and central-only."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from aiogram.types import KeyboardButtonRequestManagedBot, ReplyKeyboardRemove

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.managed_bots import ManagedBotResult


def _bot(*, manager=True):
    bot = MagicMock()
    bot.id = 888
    bot.get_me = AsyncMock(
        return_value=SimpleNamespace(id=888, can_manage_bots=manager),
    )
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=515))
    return bot


def _query(data, *, user=123):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user, username="creator", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=user)),
        data="own:" + data, answer=AsyncMock(),
    )


def _handler(router, name, *, observer="callback_query"):
    return next(
        item.callback for item in getattr(router, observer).handlers
        if item.callback.__name__ == name
    )


async def test_native_create_button_sets_durable_intent_no_token_and_no_mtproto(
    monkeypatch,
):
    actor_id = uuid4()
    flow = AsyncMock()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "set_flow", flow)
    monkeypatch.setattr(
        ui, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="configured"),
    )
    router = ui.create_owner_setup_router(MagicMock())
    bot = _bot()
    query = _query("newmanaged")
    await _handler(router, "owner_callback")(query, bot)
    flow.assert_awaited_once()
    assert flow.await_args.kwargs["actor_id"] == actor_id
    assert flow.await_args.kwargs["step"] == "managed_bot_create"
    assert flow.await_args.kwargs["bot_scope_id"] == 0
    assert 0 < flow.await_args.kwargs["payload"]["request_id"] < 2**31
    message = bot.send_message.await_args
    assert message.args[0] == 123
    assert "BotFather" not in message.args[1]
    keyboard = message.kwargs["reply_markup"]
    native = keyboard.keyboard[0][0].request_managed_bot
    assert isinstance(native, KeyboardButtonRequestManagedBot)
    assert native.request_id == flow.await_args.kwargs["payload"]["request_id"]
    assert "token" not in str(flow.await_args.kwargs["payload"]).lower()
    query.answer.assert_awaited_once()


async def test_botfather_fallback_when_manager_permission_is_off(monkeypatch):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    flow = AsyncMock()
    monkeypatch.setattr(ui, "set_flow", flow)
    monkeypatch.setattr(
        ui, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="configured"),
    )
    bot = _bot(manager=False)
    await _handler(ui.create_owner_setup_router(MagicMock()), "owner_callback")(
        _query("newmanaged"), bot,
    )
    assert "Bot Management Mode" in bot.send_message.await_args.args[1]
    flow.assert_not_awaited()


async def test_managed_notifications_are_ignored_inside_customer_bot(monkeypatch):
    called = AsyncMock(return_value=ManagedBotResult(kind="ignored"))
    monkeypatch.setattr(ui, "connect_managed_bot_event", called)
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=uuid4())
    bot = _bot()
    event = SimpleNamespace(
        user=SimpleNamespace(id=123),
        bot_user=SimpleNamespace(id=111222333),
    )
    await _handler(router, "managed_bot_updated", observer="managed_bot")(event, bot)
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        chat=SimpleNamespace(id=123, type="private"),
        managed_bot_created=SimpleNamespace(
            bot_user=SimpleNamespace(id=111222333),
        ),
    )
    await _handler(router, "managed_bot_created_message", observer="message")(msg, bot)
    called.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_both_managed_create_notifications_forward_exact_bot_and_creator(
    monkeypatch,
):
    called = AsyncMock(return_value=ManagedBotResult(kind="ignored"))
    monkeypatch.setattr(ui, "connect_managed_bot_event", called)
    router = ui.create_owner_setup_router(MagicMock())
    bot = _bot()
    event = SimpleNamespace(
        user=SimpleNamespace(id=123),
        bot_user=SimpleNamespace(id=111222333),
    )
    await _handler(router, "managed_bot_updated", observer="managed_bot")(event, bot)
    assert called.await_args.kwargs["creator_telegram_id"] == 123
    assert called.await_args.kwargs["managed_bot_id"] == 111222333
    called.reset_mock()
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=456),
        chat=SimpleNamespace(id=456, type="private"),
        managed_bot_created=SimpleNamespace(
            bot_user=SimpleNamespace(id=777888999),
        ),
    )
    await _handler(router, "managed_bot_created_message", observer="message")(msg, bot)
    assert called.await_args.kwargs["creator_telegram_id"] == 456
    assert called.await_args.kwargs["managed_bot_id"] == 777888999
    bot.send_message.assert_not_awaited()


async def test_successful_created_event_uses_connected_bot_navigation(monkeypatch):
    owner, project = uuid4(), uuid4()
    called = AsyncMock(return_value=ManagedBotResult(
        kind="created", username="fresh_vimi_bot",
        project_id=project, actor_id=owner,
    ))
    monkeypatch.setattr(ui, "connect_managed_bot_event", called)
    monkeypatch.setattr(ui, "recipient_language", _language_context)
    monkeypatch.setattr(ui, "get_project_import_status", AsyncMock(return_value=None))
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=SimpleNamespace(
        project_name="@fresh_vimi_bot",
        bots=(("fresh_vimi_bot", "registered"),),
        bot_instances=((uuid4(), "fresh_vimi_bot", "registered"),),
        bot_runtime_label="working", can_delete_bot=False,
    )))
    router = ui.create_owner_setup_router(MagicMock())
    bot = _bot()
    event = SimpleNamespace(
        user=SimpleNamespace(id=123),
        bot_user=SimpleNamespace(id=111222333),
    )
    await _handler(router, "managed_bot_updated", observer="managed_bot")(event, bot)
    assert bot.send_message.await_count >= 2
    messages = [call.args[1] for call in bot.send_message.await_args_list]
    assert any("подключён" in text for text in messages)
    assert all("123456:AA" not in text for text in messages)
    assert isinstance(bot.send_message.await_args_list[0].kwargs["reply_markup"],
                      ReplyKeyboardRemove)


class _language_context:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return False
