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
from telegram_membership_saas.modules.identity import TelegramUser

logger = logging.getLogger(__name__)

# command, Russian description, English description. Only implemented commands
# are listed; role-specific commands remain server permission checked.
ROLE_COMMANDS = {
    "buyer": (
        ("menu", "🏠 Главное меню и тарифы", "🏠 Home and plans"),
        ("subscriptions", "🧾 Мои покупки и подписки", "🧾 My purchases and subscriptions"),
        ("support", "🛟 Помощь с подпиской", "🛟 Subscription help"),
        ("language", "🌐 Язык интерфейса", "🌐 Interface language"),
        ("cancel", "⬅️ Выйти в меню", "⬅️ Return to menu"),
    ),
    "owner": (
        ("menu", "🏠 Панель управления", "🏠 Admin dashboard"),
        ("plans", "📦 Управление тарифами", "📦 Manage plans"),
        ("payments", "🧾 Заявки на оплату", "🧾 Payment reviews"),
        ("help", "❔ Помощь с настройкой", "❔ Setup help"),
        ("language", "🌐 Язык интерфейса", "🌐 Interface language"),
        ("cancel", "✖️ Отменить настройку", "✖️ Cancel setup"),
    ),
    "central": (
        ("menu", "🏠 Главное меню", "🏠 Main menu"),
        ("bots", "🤖 Мои подключённые боты", "🤖 My connected bots"),
        ("help", "❔ Как это работает", "❔ How it works"),
        ("language", "🌐 Язык интерфейса", "🌐 Interface language"),
    ),
    "platform": (
        ("platform", "👑 Панель платформы", "👑 Platform dashboard"),
        ("menu", "🏠 Личный режим", "🏠 Personal mode"),
        ("bots", "🤖 Мои подключённые боты", "🤖 My connected bots"),
        ("help", "❔ Помощь", "❔ Help"),
        ("language", "🌐 Язык интерфейса", "🌐 Interface language"),
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
        await bot.set_my_commands(commands_for(role, "en"), scope=scope)
        await bot.set_my_commands(
            commands_for(role, "ru"), scope=scope, language_code="ru",
        )
    except AiogramError:
        # Telegram Menu availability must not block polling or billing.
        logger.warning("Could not install Telegram native command menu")


async def set_chat_commands(
    bot: Bot, *, chat_id: int, role: str, language: str,
) -> None:
    """A role and preferred language override that applies immediately."""
    try:
        await bot.set_my_commands(
            commands_for(role, language),
            scope=BotCommandScopeChat(chat_id=chat_id),
        )
    except AiogramError:
        logger.warning("Could not refresh Telegram native menu for this chat")


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
    await set_chat_commands(
        bot, chat_id=chat_id, role=role, language=language,
    )
