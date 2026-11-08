"""Control plane must not expose tenant settings; owner's bot must route by role."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import (
    SetupFlow,
    SetupOverview,
)
from telegram_membership_saas.telegram_adapter import (
    create_customer_dispatcher,
)


def _message():
    return SimpleNamespace(
        chat=SimpleNamespace(type="private", id=123456),
        from_user=SimpleNamespace(
            id=123456, username="owner", first_name="Owner",
        ),
    )


def _callback(action):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=SimpleNamespace(
            id=123456, username="owner", first_name="Owner",
        ),
        answer=AsyncMock(),
    )


def _bot():
    bot = MagicMock()
    bot.id = 987654321
    bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=101),
    )
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _handler(router, name):
    return next(
        entry.callback for entry in router.callback_query.handlers
        if entry.callback.__name__ == name
    )


def _overview(project_id):
    return SetupOverview(
        project_id=project_id, project_name="My premium channel",
        timezone="Europe/Moscow",
        bots=(("rus_chan_prem_bot", "registered"),),
        resources=(), plans=(), methods=(),
        bot_instances=((uuid4(), "rus_chan_prem_bot", "registered"),),
        sales_configured=False, live_bot_username=None,
    )


def test_connected_dispatcher_includes_project_bound_admin_before_buyer():
    project_id = uuid4()
    dispatcher = create_customer_dispatcher(MagicMock(), project_id=project_id)
    assert [r.name for r in dispatcher.sub_routers] == [
        "vimi-language", "secure-owner-transfer", "project-support",
        "digital-file-sales", "manual-file-checkout", "owner-tenant", "customer-broadcasts",
        "telegram-access", "telegram-commerce",
    ]


async def test_owner_home_filter_does_not_consume_buyer_start(monkeypatch):
    project_id = uuid4()
    auth = AsyncMock(return_value=uuid4())
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(ui, "ensure_telegram_actor", auth)
    monkeypatch.setattr(ui, "can_manage_project", check)
    filt = ui.OwnerHomeFilter(MagicMock(), project_id)
    assert await filt(_message()) is False
    assert check.await_args.kwargs["project_id"] == project_id
    check.return_value = True
    assert await filt(_message()) is True
    assert await ui.OwnerHomeFilter(MagicMock(), None)(_message()) is True


async def test_central_blocks_tenant_settings_even_on_old_callback(monkeypatch):
    router = ui.create_owner_setup_router(MagicMock())
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    show = AsyncMock()
    monkeypatch.setattr(ui, "setup_overview", show)
    query = _callback("plans")
    await _handler(router, "owner_callback")(query, _bot())
    show.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_tenant_disallows_token_connection_and_pins_its_project(monkeypatch):
    project_id = uuid4()
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project_id,
    )
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    query = _callback("connect")
    await _handler(router, "owner_callback")(query, _bot())
    assert query.answer.await_args.kwargs["show_alert"] is True

    current = AsyncMock(return_value=project_id)
    monkeypatch.setattr(ui, "get_selected_project", current)
    monkeypatch.setattr(
        ui, "setup_overview", AsyncMock(return_value=_overview(project_id)),
    )
    bot = _bot()
    await _handler(router, "owner_callback")(_callback("dashboard"), bot)
    assert current.await_args.kwargs["fixed_project_id"] == project_id
    assert current.await_args.kwargs["bot_scope_id"] == bot.id
    assert "Панель администратора" in bot.send_message.await_args.args[1]
    controls = [
        b.callback_data
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "own:plans" in controls
    assert "own:connect" not in controls


async def test_connected_bot_never_allows_unauthorized_admin_callback(monkeypatch):
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=uuid4(),
    )
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    snapshot = AsyncMock()
    monkeypatch.setattr(ui, "setup_overview", snapshot)
    query = _callback("dashboard")
    await _handler(router, "owner_callback")(query, _bot())
    snapshot.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_central_registered_bot_links_to_setup_before_sales_ready(monkeypatch):
    project_id = uuid4()
    router = ui.create_owner_setup_router(MagicMock())
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=project_id),
    )
    monkeypatch.setattr(
        ui, "setup_overview", AsyncMock(return_value=_overview(project_id)),
    )
    bot = _bot()
    await _handler(router, "owner_callback")(_callback("bots"), bot)
    buttons = [
        b for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    deep_links = [b.url for b in buttons if b.url]
    assert deep_links == ["https://t.me/rus_chan_prem_bot"]
    assert "own:plans" not in [b.callback_data for b in buttons]


async def test_central_new_bot_flow_prompts_for_token_not_project_name(monkeypatch):
    owner_id = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner_id))
    monkeypatch.setattr(
        ui, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="configured"),
    )
    set_flow = AsyncMock(return_value="session-generation")
    bind = AsyncMock()
    monkeypatch.setattr(ui, "set_flow", set_flow)
    monkeypatch.setattr(ui, "bind_prompt", bind)
    bot = _bot()
    await _handler(
        ui.create_owner_setup_router(MagicMock()), "owner_callback",
    )(_callback("new"), bot)
    assert set_flow.await_args.kwargs["step"] == "new_bot_token"
    assert "BotFather" in bot.send_message.await_args.args[1]
    assert "название проекта" not in bot.send_message.await_args.args[1].lower()
    bind.assert_awaited_once()


async def test_token_first_connection_deletes_secret_and_links_owner_bot(monkeypatch):
    owner_id, project_id = uuid4(), uuid4()
    token = "123456:" + "A" * 35
    order = []

    async def delete(*_args):
        order.append("delete")
        return True

    async def register(*_args, **_kwargs):
        order.append("register")
        return project_id, SimpleNamespace(username="rus_chan_prem_bot")

    bot = _bot()
    bot.delete_message = AsyncMock(side_effect=delete)
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner_id))
    monkeypatch.setattr(
        ui, "consume_flow",
        AsyncMock(return_value=SetupFlow("new_bot_token", None, {}, 99)),
    )
    monkeypatch.setattr(
        ui, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="configured"),
    )
    monkeypatch.setattr(ui, "register_new_owner_bot", AsyncMock(side_effect=register))
    monkeypatch.setattr(
        ui, "setup_overview", AsyncMock(return_value=_overview(project_id)),
    )
    monkeypatch.setattr(ui, "get_project_import_status", AsyncMock(return_value=None))
    message = _message()
    message.text = token
    message.message_id = 199
    message.reply_to_message = SimpleNamespace(
        from_user=SimpleNamespace(id=bot.id), message_id=99,
    )
    router = ui.create_owner_setup_router(MagicMock())
    reply = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "owner_reply_handler"
    )
    await reply(message, bot)
    assert order == ["delete", "register"]
    all_messages = bot.send_message.await_args_list
    assert all(token not in str(call) for call in all_messages)
    first = all_messages[0]
    buttons = [
        b for row in first.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert [b.url for b in buttons if b.url] == [
        "https://t.me/rus_chan_prem_bot",
    ]


async def test_publish_without_payment_offers_direct_fix_not_an_alert(monkeypatch):
    project_id, plan_id, actor_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=project_id),
    )
    publish = AsyncMock(side_effect=ValueError(
        "Добавьте действующий способ оплаты для валюты тарифа"
    ))
    monkeypatch.setattr(ui, "publish_owner_plan", publish)
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project_id,
    )
    bot = _bot()
    from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid
    query = _callback("publish:" + encode_uuid(plan_id) + ":abcd1234abcd")
    await _handler(router, "owner_callback")(query, bot)
    publish.assert_awaited_once()
    outgoing = bot.send_message.await_args
    assert "Черновик и цены сохранены" in outgoing.args[1]
    controls = [
        button.callback_data
        for row in outgoing.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "own:newmethod" in controls
    assert "own:plan:" + plan_id.hex in controls
    query.answer.assert_awaited_once()


async def test_stray_botfather_token_is_deleted_in_central_only_mode(monkeypatch):
    secret = "123456:" + "A" * 35
    bot = _bot()
    message = _message()
    message.text = secret
    message.message_id = 35
    message.reply_to_message = None
    assert await ui.StrayBotTokenFilter()(message) is True
    router = ui.create_owner_setup_router(MagicMock())
    scrub = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "stray_bot_token_handler"
    )
    register = AsyncMock()
    monkeypatch.setattr(ui, "register_new_owner_bot", register)
    monkeypatch.setattr(ui, "register_customer_bot", register)
    await scrub(message, bot)
    bot.delete_message.assert_awaited_once_with(123456, 35)
    register.assert_not_awaited()
    assert secret not in str(bot.send_message.await_args)
    assert "@vimipay_bot" in bot.send_message.await_args.args[1]


async def test_failed_stray_token_delete_requires_manual_rotation():
    bot = _bot()
    bot.delete_message.return_value = False
    message = _message()
    message.text = "123456:" + "B" * 35
    message.message_id = 77
    router = ui.create_owner_setup_router(MagicMock())
    scrub = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "stray_bot_token_handler"
    )
    await scrub(message, bot)
    assert "смените токен" in bot.send_message.await_args.args[1]
    assert message.text not in str(bot.send_message.await_args)
