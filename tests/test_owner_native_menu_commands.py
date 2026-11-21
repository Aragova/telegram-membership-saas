"""Role-scoped native commands: the owner is an admin, not a buyer."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat

from telegram_membership_saas.telegram_menus import (
    commands_for,
    install_public_commands,
    set_chat_commands,
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
    bot.set_my_commands.assert_awaited_once()
    kwargs = bot.set_my_commands.await_args.kwargs
    assert isinstance(kwargs["scope"], BotCommandScopeChat)
    assert kwargs["scope"].chat_id == 345
    assert [cmd.command for cmd in kwargs["commands"]] == ["start"]
