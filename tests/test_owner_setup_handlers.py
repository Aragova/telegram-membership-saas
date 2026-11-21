"""Contract tests for the approved v2 central registrar and owner surfaces."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import (
    OwnerHome,
    SetupFlow,
    SetupOverview,
)
from telegram_membership_saas.telegram_adapter import (
    create_customer_dispatcher,
    create_dispatcher,
)


def _handler(name: str, *, callback: bool = False):
    router = ui.create_owner_setup_router(MagicMock())
    observer = router.callback_query if callback else router.message
    return next(x.callback for x in observer.handlers if x.callback.__name__ == name)


def _bot():
    bot = MagicMock()
    bot.id = 444444
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=1357)
    )
    bot.delete_message = AsyncMock(return_value=True)
    bot.set_my_commands = AsyncMock()
    return bot


def _user():
    return SimpleNamespace(
        id=123456, username="owner", first_name="Owner"
    )


def _message(text="/start"):
    return SimpleNamespace(
        from_user=_user(),
        chat=SimpleNamespace(id=123456, type="private"),
        text=text,
    )


def _overview(project_id):
    return SetupOverview(
        project_id=project_id,
        project_name="My Premium",
        timezone="Europe/Moscow",
        bots=(("my_owner_bot", "registered"),),
        resources=(),
        plans=(),
        methods=(),
    )


def test_management_dispatcher_routes_owner_first_buyer_separately():
    central = create_dispatcher(
        MagicMock(), project_id=uuid4(), enable_access=False
    )
    customer = create_customer_dispatcher(MagicMock(), project_id=uuid4())
    assert central.sub_routers[0].name == "vimi-language"
    assert central.sub_routers[1].name == "vimi-saas-proof-ingress"
    assert central.sub_routers[2].name == "platform-saas-central-stars"
    assert central.sub_routers[3].name == "platform-saas-central-gateways"
    assert central.sub_routers[4].name == "platform-saas-central-lava"
    assert central.sub_routers[5].name == "secure-owner-transfer"
    assert central.sub_routers[6].name == "central-registrar"
    assert "platform-saas-central-gateways" not in [
        r.name for r in customer.sub_routers
    ]
    assert "platform-saas-central-lava" not in [
        r.name for r in customer.sub_routers
    ]
    assert "platform-saas-central-stars" not in [r.name for r in customer.sub_routers]
    assert "owner-tenant" in [r.name for r in customer.sub_routers]
    assert "telegram-admin" not in [r.name for r in central.sub_routers]
    assert "telegram-commerce" not in [r.name for r in central.sub_routers]


async def test_botfather_secret_prompt_uses_rich_back_and_menu(monkeypatch):
    owner = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "set_flow", AsyncMock(return_value="gen-123"))
    bound = AsyncMock()
    monkeypatch.setattr(ui, "bind_prompt", bound)
    monkeypatch.setattr(
        ui, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="configured"),
    )
    callback = _handler("owner_callback", callback=True)
    bot = _bot()
    query = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=_user(), data="own:new", answer=AsyncMock(),
    )
    await callback(query, bot)
    sent = bot.send_message.await_args
    assert "BotFather" in sent.args[1]
    assert "/back" not in sent.args[1] and "/cancel" not in sent.args[1]
    buttons = [
        button.callback_data
        for row in sent.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert buttons == ["own:promptback:gen-123", "own:promptcancel:gen-123"]
    assert bound.await_args.kwargs["message_id"] == 1357
    query.answer.assert_awaited_once()


async def test_botfather_prompt_back_returns_to_subscription_choices(monkeypatch):
    owner = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    back = AsyncMock(return_value=SetupFlow(
        step="new_bot_token", project_id=None, payload={},
        prompt_message_id=1357, generation="gen-123",
    ))
    monkeypatch.setattr(ui, "back_owner_flow", back)
    bot = _bot()
    query = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=_user(), data="own:promptback:gen-123", answer=AsyncMock(),
    )
    await _handler("owner_callback", callback=True)(query, bot)
    assert back.await_args.kwargs["expected_generation"] == "gen-123"
    buttons = [
        button.callback_data
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert buttons == ["own:newmanaged", "own:new", "own:onboardhome"]
    query.answer.assert_awaited_once()


async def test_stale_rich_cancel_cannot_close_newer_owner_form(monkeypatch):
    owner = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    canceled = AsyncMock(return_value=False)
    home = AsyncMock()
    monkeypatch.setattr(ui, "cancel_owner_flow", canceled)
    monkeypatch.setattr(ui, "owner_home", home)
    bot = _bot()
    query = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=_user(), data="own:promptcancel:old", answer=AsyncMock(),
    )
    await _handler("owner_callback", callback=True)(query, bot)
    assert canceled.await_args.kwargs["expected_generation"] == "old"
    home.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_first_start_shows_only_language_choices(monkeypatch):
    uid = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uid))
    monkeypatch.setattr(
        ui, "owner_home", AsyncMock(return_value=OwnerHome((), None))
    )
    monkeypatch.setattr(
        ui, "has_explicit_language_choice", AsyncMock(return_value=False),
    )
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    bot = _bot()
    await _handler("home_message")(_message(), bot)
    sent = bot.send_message.await_args
    assert "Ваш Telegram-канал. Ваши подписчики. Ваш доход." in sent.args[1]
    assert "72 часа бесплатно" in sent.args[1]
    buttons = [
        (button.text, button.callback_data)
        for row in sent.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert buttons == [
        ("🇷🇺 Русский", "lng:set:ru"),
        ("🇬🇧 English", "lng:set:en"),
    ]
    assert "Что будем создавать?" not in sent.args[1]


async def test_first_start_after_language_has_subscription_and_back(monkeypatch):
    uid = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uid))
    monkeypatch.setattr(
        ui, "owner_home", AsyncMock(return_value=OwnerHome((), None))
    )
    monkeypatch.setattr(
        ui, "has_explicit_language_choice", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    bot = _bot()
    for command in ("/start", "/menu"):
        await _handler("home_message")(_message(command), bot)
        sent = bot.send_message.await_args
        buttons = [
            (button.text, button.callback_data)
            for row in sent.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        assert buttons == [
            ("🔐 Платные подписки", "own:substart"),
            ("⬅️ Назад", "own:onboardlanguage"),
        ]
    assert "Что будем создавать?" not in sent.args[1]
    assert "Выберите «🔐 Платные подписки»" not in sent.args[1]


async def test_subscription_choice_explains_both_connection_methods(monkeypatch):
    """The connection choice must provide real instructions, not a sales slogan."""
    from telegram_membership_saas.localization import _language

    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    callback = _handler("owner_callback", callback=True)
    bot = _bot()

    for language, expected in (
        ("ru", ("Создать бота", "Через BotFather", "API-токен", "удалим")),
        ("en", ("Create bot", "Via BotFather", "API token", "delete")),
    ):
        query = SimpleNamespace(
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            from_user=_user(), data="own:substart", answer=AsyncMock(),
        )
        token = _language.set(language)
        try:
            await callback(query, bot)
            sent = bot.send_message.await_args
            for phrase in (*expected, "/mybots", "/newbot", "@BotFather"):
                assert phrase in sent.args[1]
            assert "Продавайте доступ к закрытым каналам" not in sent.args[1]
            assert "Sell access to private channels and groups" not in sent.args[1]
            assert [
                button.callback_data for row in sent.kwargs["reply_markup"].inline_keyboard
                for button in row
            ] == ["own:newmanaged", "own:new", "own:onboardhome"]
        finally:
            _language.reset(token)


async def test_subscription_back_returns_without_existing_project(monkeypatch):
    from telegram_membership_saas.localization import _language

    uid = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uid))
    monkeypatch.setattr(
        ui, "owner_home", AsyncMock(return_value=OwnerHome((), None)),
    )
    monkeypatch.setattr(
        ui, "has_explicit_language_choice", AsyncMock(return_value=True),
    )
    selected = AsyncMock(side_effect=AssertionError("No project exists yet"))
    monkeypatch.setattr(ui, "get_selected_project", selected)
    bot = _bot()
    handler = _handler("owner_callback", callback=True)

    def query(data):
        return SimpleNamespace(
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            from_user=_user(), data=data, answer=AsyncMock(),
        )

    token = _language.set("en")
    try:
        choice = query("own:substart")
        await handler(choice, bot)
        callbacks = [
            b.callback_data for row in
            bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert callbacks == ["own:newmanaged", "own:new", "own:onboardhome"]
        assert choice.answer.await_count == 1

        go_back = query("own:onboardhome")
        await handler(go_back, bot)
        callbacks = [
            b.callback_data for row in
            bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert callbacks == ["own:substart", "own:onboardlanguage"]
        assert go_back.answer.await_count == 1
        selected.assert_not_awaited()
    finally:
        _language.reset(token)


async def test_back_from_welcome_restores_full_language_screen(monkeypatch):
    """Screen 2 Back must render screen 1, never the brief lng:menu prompt."""
    from telegram_membership_saas.localization import _language

    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    router_callback = _handler("owner_callback", callback=True)
    bot = _bot()

    for language, heading, intro, trial in (
        ("ru", "Выберите язык интерфейса:", "Ваш Telegram-канал.", "72 часа бесплатно"),
        ("en", "Choose your interface language:", "Your channel.", "72 hours free"),
    ):
        callback = SimpleNamespace(
            data="own:onboardlanguage",
            message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            from_user=_user(),
            answer=AsyncMock(),
        )
        token = _language.set(language)
        try:
            await router_callback(callback, bot)
            sent = bot.send_message.await_args
            assert intro in sent.args[1]
            assert trial in sent.args[1]
            assert heading in sent.args[1]
            # The mock Bot follows the classic HTML fallback; the same
            # screen also has a Rich-card table in Telegram clients that
            # support sendRichMessage.
            assert "<table bordered compact>" in ui.central_welcome_rich(
                language_selected=False,
            )
            assert [
                (b.text, b.callback_data)
                for row in sent.kwargs["reply_markup"].inline_keyboard for b in row
            ] == [
                ("🇷🇺 Русский", "lng:set:ru"),
                ("🇬🇧 English", "lng:set:en"),
            ]
            callback.answer.assert_awaited_once()
        finally:
            _language.reset(token)


async def test_central_start_shows_connection_registry_without_raw_uuids(monkeypatch):
    uid, project_id = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uid))
    monkeypatch.setattr(
        ui, "owner_home",
        AsyncMock(return_value=OwnerHome(((project_id, "My Premium"),), project_id)),
    )
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(return_value=_overview(project_id)),
    )
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    bot = _bot()
    await _handler("home_message")(_message(), bot)
    sent = bot.send_message.await_args
    assert "Ваши подключения" in sent.args[1]
    assert "Настройте" not in sent.args[1]
    assert project_id.hex not in sent.args[1]
    assert str(project_id) not in sent.args[1]
    assert "runtime" not in sent.args[1]


async def test_central_connected_menu_has_rich_card_and_colored_two_column_actions(
    monkeypatch,
):
    """A working connection must not show first-time onboarding instructions."""
    project = uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        ui, "owner_home",
        AsyncMock(return_value=OwnerHome(((project, "My Premium"),), project)),
    )
    monkeypatch.setattr(
        ui, "setup_overview", AsyncMock(return_value=_overview(project)),
    )
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    bot = _bot()
    await _handler("home_message")(_message(text="/menu"), bot)
    args = bot.send_message.await_args
    screen = args.args[1]
    rows = args.kwargs["reply_markup"].inline_keyboard
    assert "Ваши подключения" in screen
    assert "my_owner_bot" in screen
    assert "Подключите бота и продолжите" not in screen
    assert [len(row) for row in rows[:3]] == [2, 2, 2]
    assert [[b.style for b in row] for row in rows[:3]] == [
        ["success", "success"], ["primary", "primary"],
        ["success", "success"],
    ]
    assert rows[0][0].url == "https://t.me/my_owner_bot"
    assert rows[0][1].callback_data == "own:bots"
    assert [b.callback_data for b in rows[1]] == [
        "own:projects", "own:saasinvoices:0",
    ]
    assert [b.callback_data for b in rows[2]] == [
        "own:help", "lng:menu",
    ]


def test_central_connected_card_rich_and_classic_agree_on_navigation():
    from telegram_membership_saas.localization import _language
    from telegram_membership_saas.modules.owner_setup.premium_welcome import (
        central_connection_classic,
        central_connection_rich,
    )

    token = _language.set("ru")
    try:
        rich = central_connection_rich(
            project_name="My <Premium>", bot_username="my_owner_bot",
            runtime_label="🟢 Работает",
        )
        classic = central_connection_classic(
            project_name="My <Premium>", bot_username="my_owner_bot",
            runtime_label="🟢 Работает",
        )
        assert "<table bordered compact>" in rich
        assert "My &lt;Premium&gt;" in rich and "My &lt;Premium&gt;" in classic
        assert "https://" not in rich
        assert "тарифы и продажи" in rich
        assert "Подключите бота и продолжите" not in classic
    finally:
        _language.reset(token)

    token = _language.set("en")
    try:
        card = central_connection_rich(
            project_name="My Premium", bot_username=None,
            runtime_label="⚪ Pending",
        )
        assert "Your connections" in card and "Bot not connected" in card
        assert "Connect a bot and finish setup" not in card
    finally:
        _language.reset(token)


async def test_token_is_deleted_before_registering(monkeypatch):
    uid, project_id = uuid4(), uuid4()
    token = "123456:" + "A" * 35
    calls = []
    bot = _bot()

    async def delete(*_args):
        calls.append("delete")
        return True

    async def register(*_args, **_kwargs):
        calls.append("register")
        return SimpleNamespace(username="owned_bot")

    bot.delete_message = AsyncMock(side_effect=delete)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uid))
    monkeypatch.setattr(
        ui, "consume_flow",
        AsyncMock(return_value=SetupFlow("bot_token", project_id, {}, 999)),
    )
    monkeypatch.setattr(ui, "register_customer_bot", register)
    monkeypatch.setattr(
        ui, "set_default_project_name_from_bot", AsyncMock(),
    )
    monkeypatch.setattr(
        ui, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="key"),
    )
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(return_value=_overview(project_id)),
    )
    monkeypatch.setattr(ui, "get_project_import_status", AsyncMock(return_value=None))
    reply = _message(text=token)
    reply.message_id = 1234
    reply.reply_to_message = SimpleNamespace(
        message_id=999, from_user=SimpleNamespace(id=bot.id),
    )
    await _handler("owner_reply_handler")(reply, bot)
    assert calls == ["delete", "register"]
    assert token not in str(bot.send_message.await_args.kwargs)
    assert token not in str(bot.send_message.await_args.args)


async def test_failed_token_deletion_stops_all_registration(monkeypatch):
    uid, project_id = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uid))
    monkeypatch.setattr(
        ui, "consume_flow",
        AsyncMock(return_value=SetupFlow("bot_token", project_id, {}, 999)),
    )
    register = AsyncMock()
    monkeypatch.setattr(ui, "register_customer_bot", register)
    bot = _bot()
    bot.delete_message.return_value = False
    reply = _message(text="123456:" + "A" * 35)
    reply.message_id = 5
    reply.reply_to_message = SimpleNamespace(
        message_id=999, from_user=SimpleNamespace(id=bot.id),
    )
    await _handler("owner_reply_handler")(reply, bot)
    register.assert_not_awaited()


async def test_help_does_not_show_technical_runtime_terms(monkeypatch):
    uid, project_id = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uid))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project_id))
    bot = _bot()
    query = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=_user(), data="own:help", answer=AsyncMock(),
    )
    await _handler("owner_callback", callback=True)(query, bot)
    message = bot.send_message.await_args.args[1]
    assert "runtime" not in message
    assert str(project_id) not in message
    query.answer.assert_awaited_once()


async def test_token_sent_as_project_name_is_deleted_not_persisted(monkeypatch):
    uid = uuid4()
    raw_token = "123456:" + "A" * 35
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uid))
    monkeypatch.setattr(
        ui, "consume_flow",
        AsyncMock(return_value=SetupFlow("project_name", None, {}, 999)),
    )
    create_project = AsyncMock()
    monkeypatch.setattr(ui, "create_owner_project", create_project)
    bot = _bot()
    reply = _message(text=raw_token)
    reply.message_id = 50
    reply.reply_to_message = SimpleNamespace(
        message_id=999, from_user=SimpleNamespace(id=bot.id),
    )
    await _handler("owner_reply_handler")(reply, bot)
    bot.delete_message.assert_awaited_once()
    create_project.assert_not_awaited()
    assert raw_token not in str(bot.send_message.await_args)




async def test_stale_timezone_button_cannot_change_another_project(monkeypatch):
    actor_id, origin, selected = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id)
    )
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=selected)
    )
    change = AsyncMock()
    monkeypatch.setattr(ui, "change_project_timezone", change)
    bot = _bot()
    callback = SimpleNamespace(
        from_user=_user(), data=f"own:tz:Europe/Moscow:{origin.hex}",
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )
    await _handler("owner_callback", callback=True)(callback, bot)
    change.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True




def test_readiness_does_not_count_drafts_or_disabled_resources():
    project_id = uuid4()
    snapshot = SetupOverview(
        project_id=project_id,
        project_name="Not ready", timezone="Europe/Moscow",
        bots=(("stopped_bot", "disabled"),),
        resources=((uuid4(), "Off", "disabled"),),
        plans=((uuid4(), "Draft", "draft"),),
        methods=((uuid4(), "Bank", "disabled"),),
    )
    assert all(label.startswith("▫️") for label in ui._progress(snapshot))
    assert "📝 Черновик" == ui._status("draft")




async def test_crypto_pay_api_token_deleted_before_verification(monkeypatch):
    """Owner private setup must never echo or persist a plaintext API token."""
    owner, project = uuid4(), uuid4()
    token = "123456:" + "C" * 35
    calls = []

    async def delete(*_):
        calls.append("deleted")
        return True

    async def connected(*_args, **kwargs):
        assert calls == ["deleted"]
        assert kwargs["project_id"] == project
        assert kwargs["api_token"] == token
        assert kwargs["testnet"] is True
        calls.append("verified")

    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=SetupFlow(
        "crypto_pay_token", project, {"environment": "testnet"}, 999,
    )))
    monkeypatch.setattr(ui, "connect_crypto_pay", AsyncMock(side_effect=connected))
    monkeypatch.setattr(ui, "read_crypto_pay_connection", AsyncMock(return_value=None))
    monkeypatch.setattr(
        ui, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="test-key"),
    )
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    handler = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "owner_reply_handler"
    )
    bot = _bot()
    bot.delete_message = AsyncMock(side_effect=delete)
    message = _message(text=token)
    message.message_id = 1234
    message.reply_to_message = SimpleNamespace(
        message_id=999, from_user=SimpleNamespace(id=bot.id),
    )
    await handler(message, bot)
    assert calls == ["deleted", "verified"]
    for call in bot.send_message.await_args_list:
        assert token not in str(call)


async def test_crypto_pay_token_deletion_failure_prevents_registration(monkeypatch):
    owner, project = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=SetupFlow(
        "crypto_pay_token", project, {"environment": "mainnet"}, 999,
    )))
    connect = AsyncMock()
    monkeypatch.setattr(ui, "connect_crypto_pay", connect)
    bot = _bot()
    bot.delete_message.return_value = False
    message = _message(text="123456:" + "C" * 35)
    message.message_id = 1234
    message.reply_to_message = SimpleNamespace(
        message_id=999, from_user=SimpleNamespace(id=bot.id),
    )
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    handler = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "owner_reply_handler"
    )
    await handler(message, bot)
    connect.assert_not_awaited()
