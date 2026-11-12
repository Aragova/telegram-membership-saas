"""Prioritized confidential Crypto Pay credential ingress for connected bots.

This router is mounted before file editors and buyer/owner conversation
routers. Without it, Telegram's old Reply composer could forward a merchant
API token to a subscriber or save it as a draft file description.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from aiogram import Bot, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.crypto_pay.service import connect_crypto_pay
from telegram_membership_saas.modules.owner_setup.models import OwnerSetupSession
from telegram_membership_saas.modules.owner_setup.service import (
    consume_flow,
    ensure_telegram_actor,
)
from telegram_membership_saas.tenant_context import TenantContext


def _plausible_merchant_token(value: str | None) -> bool:
    """Reject normal buyer chat without a database query per message."""
    return bool(
        value and 20 <= len(value) <= 256 and value.isascii()
        and ":" in value and not any(ch.isspace() for ch in value)
    )


async def active_crypto_pay_secret_prompt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, bot_scope_id: int, project_id: UUID,
) -> bool:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        state = await session.get(OwnerSetupSession, (actor_id, bot_scope_id))
        return bool(
            state is not None
            and state.step == "crypto_pay_token"
            and state.selected_project_id == project_id
            and state.prompt_message_id is not None
            and state.expires_at > datetime.now(UTC)
        )


class CryptoPaySecretFilter(BaseFilter):
    def __init__(
        self, factory: async_sessionmaker[AsyncSession],
        project_id: UUID,
    ) -> None:
        self.factory = factory
        self.project_id = project_id

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if (
            message.from_user is None or message.chat.type != "private"
            or not _plausible_merchant_token(message.text)
        ):
            return False
        actor = await ensure_telegram_actor(
            self.factory, telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        return await active_crypto_pay_secret_prompt(
            self.factory, actor_id=actor, bot_scope_id=bot.id,
            project_id=self.project_id,
        )


def create_crypto_secret_router(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
) -> Router:
    router = Router(name="crypto-pay-secret-guard")

    @router.message(CryptoPaySecretFilter(factory, project_id))
    async def capture_crypto_pay_token(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.text is None:
            return
        uid = message.from_user.id
        # Never consume the setup state before Telegram confirms deletion.
        # The user's quoted older message is not trusted as a prompt ID.
        try:
            deleted = await bot.delete_message(
                message.chat.id, message.message_id,
            )
        except AiogramError:
            deleted = False
        if not deleted:
            await bot.send_message(
                uid,
                tr(
                    "⚠️ Не удалось удалить сообщение с API-токеном. "
                    "Смените токен в @CryptoBot и повторите настройку.",
                    "⚠️ Couldn't delete the message containing your API token. "
                    "Rotate it in @CryptoBot before retrying.",
                ),
            )
            return
        actor = await ensure_telegram_actor(
            factory, telegram_id=uid,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        flow = await consume_flow(
            factory, actor_id=actor,
            reply_message_id=None, bot_scope_id=bot.id,
        )
        if (
            flow is None or flow.step != "crypto_pay_token"
            or flow.project_id != project_id
            or flow.payload.get("environment") not in ("mainnet", "testnet")
        ):
            await bot.send_message(
                uid, tr("Настройка устарела. Откройте Crypto Pay заново.",
                       "Setup expired. Open Crypto Pay again."),
            )
            return
        try:
            await connect_crypto_pay(
                factory, project_id=project_id, bot_id=bot.id,
                actor_id=actor, api_token=message.text.strip(),
                encryption_key=get_settings().bot_token_encryption_key,
                testnet=flow.payload["environment"] == "testnet",
            )
        except (RuntimeError, ValueError, PermissionError):
            # Never log provider exceptions, headers, token or raw payload.
            await bot.send_message(
                uid,
                tr(
                    "⚠️ Приложение Crypto Pay не подключено. "
                    "Проверьте токен в @CryptoBot и повторите настройку.",
                    "⚠️ Crypto Pay app wasn't connected. "
                    "Check the token in @CryptoBot and try again.",
                ),
            )
            return
        await bot.send_message(
            uid,
            tr("✅ Crypto Pay проверен и защищённо подключён.",
               "✅ Crypto Pay verified and securely connected."),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr("💎 Настройки Crypto Pay", "💎 Crypto Pay settings"),
                    callback_data="own:cryptopay", style="success",
                ),
            ]]),
        )

    return router
