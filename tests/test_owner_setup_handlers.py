"""Contract tests for central ViMi Pay's owner-only setup UX."""

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
    assert central.sub_routers[0].name == "owner-setup"
    assert "owner-setup" not in [r.name for r in customer.sub_routers]
    assert "telegram-commerce" not in [r.name for r in central.sub_routers]


async def test_first_start_has_real_create_project_button(monkeypatch):
    uid = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uid))
    monkeypatch.setattr(
        ui, "owner_home", AsyncMock(return_value=OwnerHome((), None))
    )
    bot = _bot()
    await _handler("home_message")(_message(), bot)
    sent = bot.send_message.await_args
    assert "Создайте первый проект" in sent.args[1]
    assert "Создать проект" in sent.kwargs["reply_markup"].inline_keyboard[-1][0].text


async def test_owner_start_shows_configuration_progress_without_raw_uuids(monkeypatch):
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
    bot = _bot()
    await _handler("home_message")(_message(), bot)
    sent = bot.send_message.await_args
    assert "Панель владельца" in sent.args[1]
    assert "✅ Собственный бот" in sent.args[1]
    assert project_id.hex not in sent.args[1]
    assert str(project_id) not in sent.args[1]
    assert "runtime" not in sent.args[1]


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
        ui, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="key"),
    )
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(return_value=_overview(project_id)),
    )
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


