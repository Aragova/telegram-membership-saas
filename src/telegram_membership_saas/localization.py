"""RU/EN Telegram UI language: Telegram hint plus explicit per-user override.

Language belongs to a Telegram user and is shared between central and connected
bots. A language code must never be interpreted as geographic location/timezone.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.modules.identity.models import TelegramUser

logger = logging.getLogger(__name__)
_language: ContextVar[str] = ContextVar("vimi_language", default="ru")


def telegram_language(language_code: str | None) -> str:
    """Supported Telegram languages: Russian when explicitly selected; otherwise English."""
    return "ru" if (language_code or "").lower().replace("_", "-").split("-")[0] == "ru" else "en"


def current_language() -> str:
    return _language.get()


def tr(russian: str, english: str) -> str:
    return russian if current_language() == "ru" else english


def _language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lng:set:ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lng:set:en"),
    ]])


async def read_language(
    factory: async_sessionmaker[AsyncSession],
    telegram_id: int,
    telegram_hint: str | None,
    *,
    remember_hint: bool = False,
) -> str:
    """Manual preference wins, then the last observed Telegram language hint.

    The Bot API only sends the hint on incoming updates. Recording the hint lets
    outbound payment/access notifications select the recipient's own language.
    An explicit RU/EN override is never rewritten by auto-detection.
    """
    observed = telegram_language(telegram_hint) if telegram_hint else None
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    select(
                        TelegramUser.preferred_language,
                        TelegramUser.detected_language,
                    ).where(TelegramUser.telegram_user_id == telegram_id)
                )
            ).first()
            explicit, detected = row if row else (None, None)
            if remember_hint and observed is not None and detected != observed:
                await session.execute(
                    insert(TelegramUser)
                    .values(
                        telegram_user_id=telegram_id,
                        detected_language=observed,
                    )
                    .on_conflict_do_update(
                        index_elements=[TelegramUser.telegram_user_id],
                        set_={"detected_language": observed},
                    )
                )
                await session.commit()
        if explicit in ("ru", "en"):
            return explicit
        if remember_hint and observed is not None:
            return observed
        if detected in ("ru", "en"):
            return detected
    except SQLAlchemyError:
        # No localization failure must block purchases, access or admin actions.
        logger.warning("Could not read or store UI language preference")
    return telegram_language(telegram_hint)


@asynccontextmanager
async def recipient_language(
    factory: async_sessionmaker[AsyncSession] | None,
    telegram_id: int,
) -> AsyncIterator[None]:
    """Use recipient preference for unsolicited messages, never sender's locale.

    Telegram does not provide a user's live language for an outgoing message.
    Use the recipient's explicit choice or last observed Telegram language.
    Without either, keep the historical Russian default.
    """
    language = (await read_language(factory, telegram_id, "ru")
                if factory is not None else "ru")
    token = _language.set(language)
    try:
        yield
    finally:
        _language.reset(token)


class LanguageMiddleware(BaseMiddleware):
    """Set a per-update locale without exposing mutable globals between users."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def __call__(
        self, handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update, data: dict[str, Any],
    ) -> Any:
        source = event.event
        user = getattr(source, "from_user", None)
        language = (
            await read_language(
                self.factory, user.id, user.language_code, remember_hint=True,
            )
            if user is not None
            else "en"
        )
        token = _language.set(language)
        try:
            return await handler(event, data)
        finally:
            _language.reset(token)


def create_language_router(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID | None = None,
    render_main_menu: Callable[[Bot, User], Awaitable[None]] | None = None,
) -> Router:
    router = Router(name="vimi-language")

    @router.message(Command("language"))
    async def language_command(message: Message) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        await message.answer(
            tr("🌐 Выберите язык интерфейса:", "🌐 Choose your interface language:"),
            reply_markup=_language_keyboard(),
        )

    @router.callback_query(F.data == "lng:menu")
    async def language_menu(query: CallbackQuery) -> None:
        if (
            query.message is None
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer()
            return
        await query.message.answer(
            tr("🌐 Выберите язык интерфейса:", "🌐 Choose your interface language:"),
            reply_markup=_language_keyboard(),
        )
        await query.answer()

    @router.callback_query(F.data.startswith("lng:set:"))
    async def language_set(query: CallbackQuery, bot: Bot) -> None:
        if (
            query.message is None
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer()
            return
        choice = (query.data or "").removeprefix("lng:set:")
        if choice not in ("ru", "en"):
            await query.answer(tr("Неизвестный язык", "Unsupported language"), show_alert=True)
            return
        user = query.from_user
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    insert(TelegramUser)
                    .values(
                        telegram_user_id=user.id, username=user.username,
                        first_name=user.first_name, preferred_language=choice,
                    )
                    .on_conflict_do_update(
                        index_elements=[TelegramUser.telegram_user_id],
                        set_={"preferred_language": choice},
                    )
                )
        token = _language.set(choice)
        try:
            from telegram_membership_saas.telegram_menus import refresh_user_commands

            try:
                await refresh_user_commands(
                    bot, factory, chat_id=user.id, language=choice,
                    project_id=project_id,
                )
            except (SQLAlchemyError, PermissionError):
                logger.warning("Could not refresh Telegram native command menu")
            await bot.send_message(
                user.id,
                tr(
                    "✅ Русский язык включён. Открываю главное меню 👇",
                    "✅ English is on. Here’s your main menu 👇",
                ),
            )
            if render_main_menu is not None:
                await render_main_menu(bot, user)
            await query.answer()
        finally:
            _language.reset(token)

    return router
