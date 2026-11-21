"""Role-scoped native commands: the owner is an admin, not a buyer."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat

from telegram_membership_saas.telegram_menus import (
    commands_for,
    install_public_commands,
    set_chat_commands,
    sync_project_owner_commands,
)


@pytest.mark.parametrize("language", ["ru", "en"])
def test_connected_owner_has_only_start_buyer_retains_purchase_menu(language):
    owner = commands_for("owner", language)
    buyer = commands_for("buyer", language)
    assert [command.command for command in owner] == ["start"]
    assert [command.command for command in buyer] == [
        "menu", "subscription", "support",
    ]
    assert "Главное меню" in owner[0].description if language == "ru" else (
        "Main menu" in owner[0].description
    )


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_owner_native_commands_override_public_buyer_commands(language):
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    await install_public_commands(bot, customer=True)
    assert bot.set_my_commands.await_count == 2
    for call in bot.set_my_commands.await_args_list:
        assert isinstance(call.kwargs["scope"], BotCommandScopeAllPrivateChats)
        assert [cmd.command for cmd in call.kwargs["commands"]] == [
            "menu", "subscription", "support",
        ]
    bot.set_my_commands.reset_mock()
    await set_chat_commands(bot, chat_id=345, role="owner", language=language)
    assert bot.set_my_commands.await_count == 2
    default, localized = bot.set_my_commands.await_args_list
    assert "language_code" not in default.kwargs
    assert localized.kwargs["language_code"] == "ru"
    for call in (default, localized):
        kwargs = call.kwargs
        assert isinstance(kwargs["scope"], BotCommandScopeChat)
        assert kwargs["scope"].chat_id == 345
        assert [cmd.command for cmd in kwargs["commands"]] == ["start"]



async def test_deploy_repairs_existing_owner_chat_menu_without_start(monkeypatch):
    from telegram_membership_saas import telegram_menus

    project_id, owner_id = uuid4(), uuid4()
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[owner_id, 345])
    seen_projects = []

    @asynccontextmanager
    async def scope(factory, project):
        seen_projects.append(project)
        yield session

    monkeypatch.setattr(telegram_menus, "system_project_session_scope", scope)
    stored_locale = AsyncMock(return_value="en")
    monkeypatch.setattr(telegram_menus, "read_language", stored_locale)
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    await sync_project_owner_commands(bot, MagicMock(), project_id=project_id)
    assert seen_projects == [project_id]
    assert session.scalar.await_count == 2
    stored_locale.assert_awaited_once()
    assert stored_locale.await_args.args[1:] == (345, "ru")
    assert bot.set_my_commands.await_count == 2
    for call in bot.set_my_commands.await_args_list:
        assert call.kwargs["scope"].chat_id == 345
        assert [cmd.command for cmd in call.kwargs["commands"]] == ["start"]
        assert call.kwargs["commands"][0].description == "🏠 Main menu"
