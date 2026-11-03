from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.telegram_admin import handlers, service
from telegram_membership_saas.telegram_adapter import create_dispatcher
from telegram_membership_saas.tenant_context import TenantAccessDenied


def _snapshot():
    return service.AdminProjectSnapshot(
        project_name="Premium <Club>",
        timezone="Europe/Moscow",
        resources=(
            service.AdminEntry(name="Main & archive", status="active", details="Канал"),
        ),
        plans=(service.AdminEntry(name="1 month", status="active"),),
        payment_methods=(service.AdminEntry(name="Test", status="active"),),
    )


def _handler(router, name, *, callback=False):
    observer = router.callback_query if callback else router.message
    return next(item.callback for item in observer.handlers if item.callback.__name__ == name)


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return bot


def _message(*, user_id=12, text="/admin", chat_type="private"):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(type=chat_type, id=user_id),
        from_user=SimpleNamespace(id=user_id),
    )


def _callback(action, *, user_id=12, chat_type="private"):
    return SimpleNamespace(
        data=f"adm:{action}",
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat=SimpleNamespace(type=chat_type, id=user_id), message_id=91
        ),
        answer=AsyncMock(),
    )


def _router():
    return handlers.create_telegram_admin_router(None, project_id=uuid4())


def test_dispatcher_exposes_real_admin_router():
    dispatcher = create_dispatcher(MagicMock(), project_id=uuid4())
    assert "telegram-admin" in [router.name for router in dispatcher.sub_routers]


def test_admin_menu_has_no_dead_buttons_and_escapes_project_name():
    snapshot = _snapshot()
    content = handlers._screen_text("main", snapshot)
    assert "Premium &lt;Club&gt;" in content
    kb = handlers._keyboard("main", snapshot)
    actions = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert actions == [
        "adm:plans",
        "adm:resources",
        "adm:methods",
        "adm:settings",
        "adm:bots",
        "adm:reviews",
        "adm:client",
    ]
    assert "&amp;" in handlers._screen_text("resources", snapshot)


async def test_admin_menu_is_restricted(monkeypatch):
    loader = AsyncMock(side_effect=PermissionError("denied"))
    monkeypatch.setattr(handlers, "load_admin_project", loader)
    bot = _bot()
    await _handler(_router(), "admin_home_handler")(_message(), bot)
    assert "Нет доступа" in bot.send_message.await_args.kwargs["text"]


async def test_no_admin_ui_in_group(monkeypatch):
    loader = AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(handlers, "load_admin_project", loader)
    bot = _bot()
    await _handler(_router(), "admin_home_handler")(
        _message(chat_type="supergroup"), bot
    )
    loader.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_owner_sees_project_and_can_open_settings(monkeypatch):
    loader = AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(handlers, "load_admin_project", loader)
    bot = _bot()
    await _handler(_router(), "admin_home_handler")(_message(), bot)
    assert "ViMi Pay" in bot.send_message.await_args.kwargs["text"]
    callback = _callback("settings")
    await _handler(_router(), "admin_navigation_handler", callback=True)(callback, bot)
    assert "Текущий часовой пояс" in bot.edit_message_text.await_args.kwargs["text"]
    callback.answer.assert_awaited_once()


async def test_central_start_shows_owner_bots_without_admin_command(monkeypatch):
    loader = AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(handlers, "load_admin_project", loader)
    bot = _bot()
    await _handler(_router(), "admin_home_handler")(
        _message(text="/start"), bot
    )
    sent = bot.send_message.await_args.kwargs
    assert "Мои боты" in sent["text"]
    assert "Подключить своего бота" in (
        sent["reply_markup"].inline_keyboard[0][0].text
    )
    assert sent["chat_id"] == 12
    loader.assert_awaited_once()


async def test_central_menu_command_opens_overview(monkeypatch):
    monkeypatch.setattr(
        handlers, "load_admin_project", AsyncMock(return_value=_snapshot())
    )
    bot = _bot()
    await _handler(_router(), "admin_home_handler")(
        _message(text="/menu"), bot
    )
    assert "ViMi Pay · Управление" in bot.send_message.await_args.kwargs["text"]


async def test_unregistered_start_does_not_show_buyer_catalog(monkeypatch):
    loader = AsyncMock(side_effect=PermissionError("denied"))
    catalog = AsyncMock()
    monkeypatch.setattr(handlers, "load_admin_project", loader)
    monkeypatch.setattr(handlers, "_send_catalog", catalog)
    bot = _bot()
    await _handler(_router(), "admin_home_handler")(
        _message(user_id=999, text="/start"), bot
    )
    text = bot.send_message.await_args.kwargs["text"]
    assert "центральный бот" in text
    assert "не бот покупки подписок" in text
    assert "нет доступа" in text
    catalog.assert_not_awaited()


async def test_owner_changes_timezone_by_button_and_command(monkeypatch):
    loader = AsyncMock(return_value=_snapshot())
    change = AsyncMock(return_value="Europe/Amsterdam")
    monkeypatch.setattr(handlers, "load_admin_project", loader)
    monkeypatch.setattr(handlers, "change_project_timezone", change)
    bot = _bot()
    callback = _callback("tz:Europe/Amsterdam")
    await _handler(_router(), "admin_navigation_handler", callback=True)(callback, bot)
    assert change.await_args.kwargs["timezone_name"] == "Europe/Amsterdam"
    change.reset_mock()
    await _handler(_router(), "timezone_command_handler")(
        _message(text="/timezone Asia/Tokyo"), bot
    )
    assert change.await_args.kwargs["timezone_name"] == "Asia/Tokyo"


async def test_forged_callback_cannot_change_timezone(monkeypatch):
    change = AsyncMock(side_effect=PermissionError("denied"))
    loader = AsyncMock(return_value=_snapshot())
    monkeypatch.setattr(handlers, "load_admin_project", loader)
    monkeypatch.setattr(handlers, "change_project_timezone", change)
    bot = _bot()
    callback = _callback("tz:Europe/London", user_id=777)
    await _handler(_router(), "admin_navigation_handler", callback=True)(callback, bot)
    assert callback.answer.await_args.kwargs["show_alert"] is True
    bot.edit_message_text.assert_not_awaited()


async def test_timezone_requires_real_iana_zone(monkeypatch):
    user = AsyncMock()
    monkeypatch.setattr(service, "_actor_id", user)
    with pytest.raises(ValueError, match="Неизвестный часовой пояс"):
        await service.change_project_timezone(
            None, project_id=uuid4(), actor_telegram_user_id=99, timezone_name="../etc"
        )
    user.assert_not_awaited()


def _session(project, role=None):
    session = SimpleNamespace()
    session.scalar = AsyncMock(side_effect=[project, role])
    session.flush = AsyncMock()
    return session


async def test_owner_timezone_change_is_persisted_in_tenant_scope(monkeypatch):
    actor_id = uuid4()
    project = SimpleNamespace(
        owner_user_id=actor_id, status="active", timezone="Europe/Moscow"
    )
    session = _session(project)

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(service, "_actor_id", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(service, "tenant_session_scope", scope)
    result = await service.change_project_timezone(
        None,
        project_id=uuid4(),
        actor_telegram_user_id=12,
        timezone_name="Europe/Amsterdam",
    )
    assert result == "Europe/Amsterdam"
    assert project.timezone == "Europe/Amsterdam"
    session.flush.assert_awaited_once()
    assert session.scalar.await_count == 1


async def test_nonmanager_cannot_change_project_timezone(monkeypatch):
    actor_id = uuid4()
    project = SimpleNamespace(
        owner_user_id=uuid4(), status="active", timezone="Europe/Moscow"
    )
    session = _session(project, role="viewer")

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(service, "_actor_id", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(service, "tenant_session_scope", scope)
    with pytest.raises(PermissionError):
        await service.change_project_timezone(
            None,
            project_id=uuid4(),
            actor_telegram_user_id=12,
            timezone_name="Europe/Amsterdam",
        )
    assert project.timezone == "Europe/Moscow"
    session.flush.assert_not_awaited()


async def test_tenant_scope_denial_is_not_leaked(monkeypatch):
    @asynccontextmanager
    async def denied(*_args, **_kwargs):
        raise TenantAccessDenied("private project id")
        yield

    monkeypatch.setattr(service, "_actor_id", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(service, "tenant_session_scope", denied)
    with pytest.raises(PermissionError, match="Нет доступа"):
        await service.load_admin_project(
            None, project_id=uuid4(), actor_telegram_user_id=111
        )
