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


async def test_rich_creation_step_precedes_temporary_native_button(monkeypatch):
    actor_id = uuid4()
    flow = AsyncMock()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "set_flow", flow)
    monkeypatch.setattr(
        ui, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="configured"),
    )
    router = ui.create_owner_setup_router(MagicMock())
    handler = _handler(router, "owner_callback")
    bot = _bot()

    # Screen 4 uses in-message buttons, not a sticky system keyboard.
    first = _query("newmanaged")
    await handler(first, bot)
    flow.assert_not_awaited()
    sent = bot.send_message.await_args
    assert any(title in sent.args[1] for title in (
        "Создать бота через Telegram", "Create a bot with Telegram",
    ))
    assert "@username" in sent.args[1]
    assert "BotFather" not in sent.args[1]
    assert [
        b.callback_data for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row
    ] == ["own:managedlaunch", "own:substart"]
    first.answer.assert_awaited_once()

    # Only the explicit second click reveals Telegram's required reply button.
    second = _query("managedlaunch")
    await handler(second, bot)
    flow.assert_awaited_once()
    assert flow.await_args.kwargs["actor_id"] == actor_id
    assert flow.await_args.kwargs["step"] == "managed_bot_create"
    assert flow.await_args.kwargs["bot_scope_id"] == 0
    assert 0 < flow.await_args.kwargs["payload"]["request_id"] < 2**31
    native_message = bot.send_message.await_args
    assert native_message.args[0] == 123
    native_keyboard = native_message.kwargs["reply_markup"]
    assert native_keyboard.one_time_keyboard is True
    native = native_keyboard.keyboard[0][0].request_managed_bot
    assert isinstance(native, KeyboardButtonRequestManagedBot)
    assert native.request_id == flow.await_args.kwargs["payload"]["request_id"]
    assert "token" not in str(flow.await_args.kwargs["payload"]).lower()
    second.answer.assert_awaited_once()


async def test_rich_creation_back_returns_to_subscription_choices(monkeypatch):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "get_settings", lambda: SimpleNamespace(
        bot_token_encryption_key="configured",
    ))
    bot = _bot()
    handler = _handler(ui.create_owner_setup_router(MagicMock()), "owner_callback")
    await handler(_query("newmanaged"), bot)
    back = _query("substart")
    await handler(back, bot)
    keyboard = bot.send_message.await_args.kwargs["reply_markup"]
    buttons = [
        b.callback_data for row in keyboard.inline_keyboard
        for b in row
    ]
    assert buttons == ["own:newmanaged", "own:new", "own:onboardhome"]


async def test_native_launch_checks_management_permission_again(monkeypatch):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "set_flow", AsyncMock())
    monkeypatch.setattr(ui, "get_settings", lambda: SimpleNamespace(
        bot_token_encryption_key="configured",
    ))
    bot = _bot(manager=False)
    await _handler(ui.create_owner_setup_router(MagicMock()), "owner_callback")(
        _query("managedlaunch"), bot,
    )
    ui.set_flow.assert_not_awaited()
    text = bot.send_message.await_args.args[1]
    assert "Bot Management Mode" in text or "Режим управления ботами" in text


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


async def test_managed_service_message_ignored_inside_customer_bot(monkeypatch):
    staged = AsyncMock(return_value="generation")
    monkeypatch.setattr(ui, "stage_managed_bot_candidate", staged)
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=uuid4())
    bot = _bot()
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        chat=SimpleNamespace(id=123, type="private"),
        managed_bot_created=SimpleNamespace(
            bot_user=SimpleNamespace(id=111222333, username="created_bot"),
        ),
    )
    await _handler(router, "managed_bot_created_message", observer="message")(msg, bot)
    staged.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_managed_create_service_message_offers_confirmation_without_token(
    monkeypatch,
):
    actor_id = uuid4()
    staged = AsyncMock(return_value="generation1")
    called = AsyncMock(return_value=ManagedBotResult(kind="ignored"))
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "stage_managed_bot_candidate", staged)
    monkeypatch.setattr(ui, "connect_managed_bot_event", called)
    monkeypatch.setattr(ui, "recipient_language", _language_context)
    router = ui.create_owner_setup_router(MagicMock())
    bot = _bot()
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=456, username="owner", first_name="Owner"),
        chat=SimpleNamespace(id=456, type="private"),
        managed_bot_created=SimpleNamespace(
            bot_user=SimpleNamespace(id=777888999, username="new_sales_bot"),
        ),
    )
    await _handler(router, "managed_bot_created_message", observer="message")(msg, bot)
    staged.assert_awaited_once()
    assert staged.await_args.kwargs == {
        "actor_id": actor_id, "managed_bot_id": 777888999,
    }
    called.assert_not_awaited()
    send = bot.send_message.await_args
    assert "@new_sales_bot" in send.args[1]
    buttons = [
        x.callback_data for row in send.kwargs["reply_markup"].inline_keyboard
        for x in row
    ]
    assert "own:managedconfirm:777888999:generation1" in buttons
    assert all("token" not in str(x).lower() for x in buttons)


async def test_managed_confirm_callback_connects_selected_bot_and_clears_keyboard(
    monkeypatch,
):
    owner, project = uuid4(), uuid4()
    called = AsyncMock(return_value=ManagedBotResult(
        kind="created", username="fresh_vimi_bot",
        project_id=project, actor_id=owner,
    ))
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "connect_managed_bot_event", called)
    monkeypatch.setattr(ui, "get_settings", lambda: SimpleNamespace(
        bot_token_encryption_key="configured",
    ))
    monkeypatch.setattr(ui, "get_project_import_status", AsyncMock(return_value=None))
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=SimpleNamespace(
        project_name="@fresh_vimi_bot",
        bots=(("fresh_vimi_bot", "registered"),),
        bot_instances=((uuid4(), "fresh_vimi_bot", "registered"),),
        bot_runtime_label="working", can_delete_bot=False,
    )))
    bot = _bot()
    await _handler(ui.create_owner_setup_router(MagicMock()), "owner_callback")(
        _query("managedconfirm:111222333:gen"), bot,
    )
    called.assert_awaited_once()
    assert called.await_args.kwargs["managed_bot_id"] == 111222333
    assert called.await_args.kwargs["generation"] == "gen"
    assert bot.send_message.await_count >= 2
    messages = [call.args[1] for call in bot.send_message.await_args_list]
    assert any("подключён" in text for text in messages)
    assert isinstance(bot.send_message.await_args_list[0].kwargs["reply_markup"],
                      ReplyKeyboardRemove)



async def test_managed_confirm_acknowledges_before_external_token_lookup(monkeypatch):
    """A slow Telegram getManagedBotToken must not expire the callback."""
    owner = uuid4()
    query = _query("managedconfirm:111222333:generation1")
    bot = _bot()

    async def failed_connect(*_args, **_kwargs):
        query.answer.assert_awaited_once()
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "connect_managed_bot_event", failed_connect)
    monkeypatch.setattr(ui, "get_settings", lambda: SimpleNamespace(
        bot_token_encryption_key="configured",
    ))

    await _handler(ui.create_owner_setup_router(MagicMock()), "owner_callback")(
        query, bot,
    )

    assert query.answer.await_count == 1
    assert bot.send_message.await_count == 1
    assert "Попробуйте" in bot.send_message.await_args.args[1]
    assert "provider unavailable" not in bot.send_message.await_args.args[1]


async def test_managed_confirm_shows_billing_or_registration_rejection(monkeypatch):
    owner = uuid4()
    query = _query("managedconfirm:111222333:generation1")
    bot = _bot()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "connect_managed_bot_event", AsyncMock(
        side_effect=ValueError("Подключение нового бота временно ограничено"),
    ))
    monkeypatch.setattr(ui, "get_settings", lambda: SimpleNamespace(
        bot_token_encryption_key="configured",
    ))

    await _handler(ui.create_owner_setup_router(MagicMock()), "owner_callback")(
        query, bot,
    )

    query.answer.assert_awaited_once()
    assert bot.send_message.await_count == 1
    assert "⚠️" in bot.send_message.await_args.args[1]


async def test_managed_confirm_reports_internal_failure_without_leaking_tokens(
    monkeypatch, caplog,
):
    owner = uuid4()
    query = _query("managedconfirm:111222333:generation1")
    bot = _bot()
    secret = "123456789:credential-never-appears"
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "connect_managed_bot_event", AsyncMock(
        side_effect=OSError(secret),
    ))
    monkeypatch.setattr(ui, "get_settings", lambda: SimpleNamespace(
        bot_token_encryption_key="configured",
    ))

    await _handler(ui.create_owner_setup_router(MagicMock()), "owner_callback")(
        query, bot,
    )

    query.answer.assert_awaited_once()
    assert bot.send_message.await_count == 1
    assert "внутренней ошибки" in bot.send_message.await_args.args[1]
    assert secret not in caplog.text
    assert secret not in bot.send_message.await_args.args[1]


async def test_managed_confirm_expired_or_replayed_request_is_visible(monkeypatch):
    owner = uuid4()
    query = _query("managedconfirm:111222333:generation1")
    bot = _bot()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "connect_managed_bot_event", AsyncMock(
        return_value=ManagedBotResult(kind="ignored"),
    ))
    monkeypatch.setattr(ui, "get_settings", lambda: SimpleNamespace(
        bot_token_encryption_key="configured",
    ))

    await _handler(ui.create_owner_setup_router(MagicMock()), "owner_callback")(
        query, bot,
    )

    query.answer.assert_awaited_once()
    assert bot.send_message.await_count == 1
    assert "Мои подключения" in bot.send_message.await_args.args[1]


async def test_managed_confirm_preserves_staged_wizard_through_callback_middleware(
    monkeypatch,
):
    """Real callback middleware must not clear the candidate before confirmation."""
    actor_id = uuid4()
    ensure = AsyncMock(return_value=actor_id)
    cancel = AsyncMock()
    monkeypatch.setattr(ui, "ensure_telegram_actor", ensure)
    monkeypatch.setattr(ui, "cancel_owner_flow", cancel)
    middleware = ui.ClearOwnerInputOnNavigation(MagicMock(), None)
    handler = AsyncMock(return_value="handled")

    result = await middleware(
        handler, _query("managedconfirm:777888999:generation1"),
        {"bot": _bot()},
    )

    assert result == "handled"
    handler.assert_awaited_once()
    ensure.assert_not_awaited()
    cancel.assert_not_awaited()


async def test_other_navigation_still_cancels_owner_wizard(monkeypatch):
    """Only the confirmation exception is added; ordinary navigation still clears input."""
    actor_id = uuid4()
    ensure = AsyncMock(return_value=actor_id)
    cancel = AsyncMock()
    monkeypatch.setattr(ui, "ensure_telegram_actor", ensure)
    monkeypatch.setattr(ui, "cancel_owner_flow", cancel)
    middleware = ui.ClearOwnerInputOnNavigation(MagicMock(), None)
    handler = AsyncMock(return_value=None)

    await middleware(handler, _query("projects"), {"bot": _bot()})

    cancel.assert_awaited_once_with(
        middleware.factory, actor_id=actor_id, bot_scope_id=0,
    )
    handler.assert_awaited_once()

class _language_context:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return False
