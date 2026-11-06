"""Small but important owner/buyer UX contract: no forced replies or dead menus."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from aiogram.types import ReplyKeyboardRemove

from telegram_membership_saas import localization
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import SetupFlow


def owner_bot():
    bot = MagicMock()
    bot.id = 765432
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=999),
    )
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def owner_query(action):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(
            id=42, username="owner", first_name="Owner",
        ),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=42),
            message_id=100,
        ),
        answer=AsyncMock(),
    )


def owner_callbacks(monkeypatch, project):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(
        return_value=True,
    ))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(
        return_value=project,
    ))
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )


async def test_new_plan_starts_at_duration_and_custom_name_is_optional(monkeypatch):
    project = uuid4()
    callback = owner_callbacks(monkeypatch, project)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=SimpleNamespace(
            resources=((uuid4(), "Channel", "active"),),
        ),
    ))
    create_flow = AsyncMock(return_value="gen1")
    monkeypatch.setattr(ui, "set_flow", create_flow)
    bot = owner_bot()
    await callback(owner_query("newplan"), bot)
    assert create_flow.await_args.kwargs["step"] == "plan_duration"
    assert create_flow.await_args.kwargs["payload"] == {"name": ""}
    sent = bot.send_message.await_args
    assert "необязательно" in sent.args[1]
    buttons = [
        b for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any(b.callback_data == "own:duration:month:gen1" for b in buttons)
    assert any(b.callback_data == "own:planname:gen1" for b in buttons)

    consume = AsyncMock(return_value=SetupFlow(
        step="plan_duration", project_id=project,
        payload={"name": ""}, prompt_message_id=None, generation="gen1",
    ))
    monkeypatch.setattr(ui, "consume_button_flow", consume)
    monkeypatch.setattr(ui, "advance_flow", AsyncMock(
        return_value="gen2",
    ))
    bind = AsyncMock()
    monkeypatch.setattr(ui, "bind_prompt", bind)
    await callback(owner_query("planname:gen1"), bot)
    assert ui.advance_flow.await_args.kwargs["step"] == "plan_name"
    assert isinstance(
        bot.send_message.await_args.kwargs["reply_markup"],
        ReplyKeyboardRemove,
    )
    bind.assert_awaited_once()


async def test_plain_text_name_reply_without_force_reply_composer(monkeypatch):
    project, actor = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=actor,
    ))
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(
        return_value=SetupFlow(
            step="plan_name", project_id=project, payload={},
            prompt_message_id=999, generation="gen",
        ),
    ))
    monkeypatch.setattr(ui, "advance_flow", AsyncMock(return_value="gen2"))
    bot = owner_bot()
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    reply = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "owner_reply_handler"
    )
    message = SimpleNamespace(
        text="VIP Premium", message_id=1000,
        chat=SimpleNamespace(type="private", id=42),
        from_user=SimpleNamespace(
            id=42, username="owner", first_name="Owner",
        ),
        reply_to_message=None,
    )
    await reply(message, bot)
    assert ui.consume_flow.await_args.kwargs["reply_message_id"] is None
    assert ui.advance_flow.await_args.kwargs["payload"]["name"] == "VIP Premium"
    assert "Выберите срок" in bot.send_message.await_args.args[1]


async def test_setup_reply_filter_accepts_plain_text_only_for_active_owner(monkeypatch):
    project, actor = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=actor,
    ))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    current = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "is_expected_prompt", current)
    filter_ = ui.SetupReplyFilter(MagicMock(), project)
    bot = owner_bot()
    message = SimpleNamespace(
        text="Pay to bank", chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(
            id=42, username="owner", first_name="Owner",
        ),
        reply_to_message=None,
    )
    assert await filter_(message, bot)
    assert current.await_args.kwargs["reply_message_id"] is None
    current.reset_mock()
    message.text = "/menu"
    assert not await filter_(message, bot)
    current.assert_not_awaited()
    message.text = "regular text"
    ui.can_manage_project.return_value = False
    assert not await filter_(message, bot)
    current.assert_not_awaited()


async def test_language_change_opens_main_menu_automatically(monkeypatch):
    session = MagicMock()
    session.execute = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    import telegram_membership_saas.telegram_menus as native

    update_commands = AsyncMock()
    monkeypatch.setattr(native, "refresh_user_commands", update_commands)
    menu = AsyncMock()

    async def render_main(bot, user):
        assert localization.current_language() == "en"
        await menu(bot, user)

    router = localization.create_language_router(
        factory, project_id=uuid4(),
        render_main_menu=render_main,
    )
    language_set = next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "language_set"
    )
    query = SimpleNamespace(
        data="lng:set:en",
        from_user=SimpleNamespace(
            id=42, username="buyer", first_name="Buyer",
        ),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=42),
        ),
        answer=AsyncMock(),
    )
    bot = owner_bot()
    await language_set(query, bot)
    update_commands.assert_awaited_once()
    menu.assert_awaited_once()
    query.answer.assert_awaited_once()
    assert "main menu" in bot.send_message.await_args.args[1]
    assert localization.current_language() == "ru"


async def test_navigation_middleware_discards_plain_input_on_dashboard_click(monkeypatch):
    project, actor = uuid4(), uuid4()
    factory = MagicMock()
    middleware = ui.ClearOwnerInputOnNavigation(factory, project)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=actor,
    ))
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "cancel_owner_flow", cancel)
    cancel_broadcast = AsyncMock()
    monkeypatch.setattr(ui, "cancel_draft", cancel_broadcast)
    handler = AsyncMock(return_value="handled")
    event = owner_query("dashboard")
    bot = owner_bot()
    assert await middleware(handler, event, {"bot": bot}) == "handled"
    cancel.assert_awaited_once_with(
        factory, actor_id=actor, bot_scope_id=bot.id,
    )
    handler.assert_awaited_once()
    cancel_broadcast.assert_awaited_once_with(
        factory, project_id=project, actor_id=actor,
    )

    # Wizard buttons are generation-scoped, handled atomically by the
    # existing consume_button_flow; navigation middleware leaves them intact.
    cancel.reset_mock()
    event.data = "own:duration:month:gen"
    await middleware(handler, event, {"bot": bot})
    cancel.assert_not_awaited()

    event.data = "own:planname:gen"
    await middleware(handler, event, {"bot": bot})
    cancel.assert_not_awaited()

    # Group callback cannot cancel a private wizard.
    event.data = "own:dashboard"
    event.message.chat.type = "supergroup"
    await middleware(handler, event, {"bot": bot})
    cancel.assert_not_awaited()
