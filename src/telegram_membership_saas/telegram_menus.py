"""Role-aware native Telegram Menu commands, not reply keyboards.

The visible command list is never a permission grant. Every handler performs
its own authorization checks. User language overrides Telegram app language.
"""
from __future__ import annotations

import logging
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import AiogramError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.identity import TelegramUser

logger = logging.getLogger(__name__)

# command, Russian description, English description. Only implemented commands
# are listed; role-specific commands remain server permission checked.
ROLE_COMMANDS = {
    # Buyer-only native menu; admin commands use a per-chat role override.
    # Contextual buyer actions remain on existing screens and via commands.
    "buyer": (
        ("menu", "🏠 Главное меню", "🏠 Main menu"),
        ("subscription", "🧾 Мои подписки", "🧾 My subscriptions"),
        ("support", "🛟 Поддержка", "🛟 Support"),
    ),
    # In the connected bot's ADMIN chat, display only the admin entrypoint.
    # Buyers retain their separate public commands; handlers still validate roles.
    "owner": (
        ("start", "🏠 Главное меню", "🏠 Main menu"),
    ),
    "payment_manager": (
        ("menu", "🏠 Главное меню", "🏠 Main menu"),
        ("filepayments", "🧾 Проверка оплат файлов", "🧾 File payment reviews"),
        ("support", "🛟 Поддержка", "🛟 Support"),
    ),
    # Central and platform owner menus expose only the 3 stable entrypoints.
    # /menu and /help remain valid hidden commands for old messages.
    # /billing must survive removal of the final bot.
    "central": (
        ("start", "🏠 Главное меню", "🏠 Main menu"),
        ("bots", "🤖 Мои подключения", "🤖 My connections"),
        ("billing", "🧾 Счёт аккаунта", "🧾 Account bill"),
    ),
    "platform": (
        ("start", "🏠 Главное меню", "🏠 Main menu"),
        ("bots", "🤖 Мои подключения", "🤖 My connections"),
        ("billing", "🧾 Счёт аккаунта", "🧾 Account bill"),
    ),
}


def commands_for(role: str, language: str) -> list[BotCommand]:
    if role not in ROLE_COMMANDS:
        raise ValueError("Unknown command menu role")
    return [
        BotCommand(command=name, description=ru if language == "ru" else en)
        for name, ru, en in ROLE_COMMANDS[role]
    ]


async def install_public_commands(bot: Bot, *, customer: bool) -> None:
    """Register an English fallback and RU native menu for private chats."""
    role = "buyer" if customer else "central"
    scope = BotCommandScopeAllPrivateChats()
    try:
        await bot.set_my_commands(commands=commands_for(role, "en"), scope=scope)
        await bot.set_my_commands(
            commands=commands_for(role, "ru"), scope=scope, language_code="ru",
        )
    except AiogramError:
        # Telegram Menu availability must not block polling or billing.
        logger.warning("Could not install Telegram native command menu")


async def set_chat_commands(
    bot: Bot, *, chat_id: int, role: str, language: str,
) -> None:
    """A role and preferred language override that applies immediately."""
    # A private-chat language-specific command list can persist independently
    # of the neutral list. Explicitly overwrite both for the exact chat to
    # avoid keeping old /subscription buyer commands in an owner chat.
    scope = BotCommandScopeChat(chat_id=chat_id)
    for language_code in (None, "ru"):
        try:
            await bot.set_my_commands(
                commands=commands_for(role, language),
                scope=scope,
                **({"language_code": language_code}
                   if language_code is not None else {}),
            )
        except AiogramError:
            logger.warning(
                "Could not refresh %s Telegram commands for chat %s",
                language_code or "default", chat_id,
            )


async def refresh_user_commands(
    bot: Bot,
    factory: async_sessionmaker[AsyncSession],
    *,
    chat_id: int,
    language: str,
    project_id: UUID | None = None,
) -> None:
    """Refresh menu after /language, using fresh authorization every time."""
    if project_id is None:
        role = (
            "platform"
            if chat_id == get_settings().platform_owner_telegram_id
            else "central"
        )
    else:
        async with factory() as session:
            actor_id = await session.scalar(
                select(TelegramUser.id).where(
                    TelegramUser.telegram_user_id == chat_id,
                )
            )
        role = "buyer"
        if actor_id is not None:
            # The menu never *grants* permission; authorization is rechecked.
            from telegram_membership_saas.modules.owner_setup.service import (
                can_manage_project,
            )

            if await can_manage_project(
                factory, actor_id=actor_id, project_id=project_id,
            ):
                role = "owner"
            else:
                from telegram_membership_saas.modules.file_sales.manual_service import (
                    can_review_file_payments,
                )
                if await can_review_file_payments(
                    factory, project_id=project_id, actor_id=actor_id,
                ):
                    role = "payment_manager"
    await set_chat_commands(
        bot, chat_id=chat_id, role=role, language=language,
    )



async def sync_project_owner_commands(
    bot: Bot,
    factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
) -> None:
    """Repair already-connected owner menus during customer bot startup.

    setMyCommands(AllPrivateChats) installs the buyer list for everyone. A
    narrower chat-scoped owner list must be recreated after deployments even
    when the owner doesn't send /start. No owner identity is guessed from a
    Telegram username; it comes from the project record in project-scoped RLS.
    """
    async with system_project_session_scope(factory, project_id) as session:
        owner_id = await session.scalar(
            select(Project.owner_user_id).where(Project.id == project_id),
        )
        telegram_id = (
            await session.scalar(
                select(TelegramUser.telegram_user_id).where(
                    TelegramUser.id == owner_id,
                ),
            )
            if owner_id is not None else None
        )
    if telegram_id is not None:
        await set_chat_commands(
            bot, chat_id=telegram_id, role="owner", language="ru",
        )
