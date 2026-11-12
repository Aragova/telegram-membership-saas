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
    SetupFlow,
    consume_button_flow,
    ensure_telegram_actor,
)
from telegram_membership_saas.tenant_context import TenantContext


def _plausible_merchant_token(value: str | None) -> bool:
    """Reject normal buyer chat without a database query per message."""
    # Telegram paste commonly includes surrounding spaces/newlines.
    # Classify the same normalized text the credential handler consumes,
    # BEFORE any earlier file/transfer/buyer-message router can see it.
    cleaned = value.strip() if isinstance(value, str) else ""
    # Ordinary https:// channel/support links must remain available to
    # customer chat; they are not merchant API credentials.
    if cleaned.lower().startswith(("https://", "http://", "tg://")):
        return False
    if (
        not 20 <= len(cleaned) <= 256
        or not cleaned.isascii()
        or any(ch.isspace() for ch in cleaned)
    ):
        return False
    # No unconditional alphanumeric-only detection. Wallet addresses
    # (Ethereum/TRON) and transaction hashes are PUBLIC buyer payment data,
    # and must never be deleted from a support conversation.
    # Outside a live wizard, only numeric-prefix, colon-delimited Crypto
    # Pay credentials are scrubbed. Inside a live wizard ANY input is deleted
    # first, so provider token variations remain fully supported there.
    prefix, separator, secret = cleaned.partition(":")
    return bool(
        separator and prefix.isascii() and prefix.isdecimal()
        and 1 <= len(prefix) <= 20
        and len(secret) >= 15 and not any(ch.isspace() for ch in secret)
    )


async def read_crypto_pay_secret_prompt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, bot_scope_id: int, project_id: UUID,
) -> SetupFlow | None:
    """Read the live wizard WITHOUT consuming it before provider verification.

    A failed Crypto Pay getMe MUST leave the protected prompt active; otherwise
    a retry quoting a stale buyer message could forward the merchant secret.
    The original generation prevents completion of a newer unrelated wizard.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        state = await session.get(OwnerSetupSession, (actor_id, bot_scope_id))
        if (
            state is None
            or state.step != "crypto_pay_token"
            or state.selected_project_id != project_id
            or state.prompt_message_id is None
            or state.generation is None
            or state.expires_at <= datetime.now(UTC)
            or state.payload.get("environment") not in ("mainnet", "testnet")
        ):
            return None
        return SetupFlow(
            step=state.step, project_id=state.selected_project_id,
            payload=dict(state.payload),
            prompt_message_id=state.prompt_message_id,
            generation=state.generation,
        )


async def active_crypto_pay_secret_prompt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, bot_scope_id: int, project_id: UUID,
) -> bool:
    return await read_crypto_pay_secret_prompt(
        factory, actor_id=actor_id, bot_scope_id=bot_scope_id,
        project_id=project_id,
    ) is not None


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
            or message.text is None or message.text.startswith("/")
        ):
            return False
        # A token-shaped text is ALWAYS deleted, even after the 15-minute
        # wizard expired or after a successful setup. This prevents secrets
        # from escaping into old buyer replies when a prompt is stale.
        if _plausible_merchant_token(message.text):
            return True
        # While the wizard is active, intercept even a malformed/truncated
        # first attempt, so an alternate owner reply handler cannot consume
        # the form and leave a subsequent valid retry unprotected.
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
        flow = await read_crypto_pay_secret_prompt(
            factory, actor_id=actor,
            bot_scope_id=bot.id, project_id=project_id,
        )
        if flow is None:
            await bot.send_message(
                uid, tr(
                    "🔒 Сообщение с возможным API-токеном удалено. "
                    "Защищённая настройка не активна. Откройте 💎 Crypto Pay "
                    "в способах оплаты и начните подключение заново.",
                    "🔒 A possible API token was deleted. "
                    "Protected setup isn't active. Open 💎 Crypto Pay "
                    "from payment methods and start a new connection.",
                ),
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
            # Do not consume or invalidate the original secret prompt on a
            # failed provider check. Retrying with the same old Telegram
            # reply target MUST still hit this router before buyer replies.
            # Never include provider exceptions or the token in responses.
            await bot.send_message(
                uid,
                tr(
                    "⚠️ Crypto Pay не подтвердил приложение. "
                    "Защищённый ввод остаётся активным. Проверьте API-токен "
                    "в @CryptoBot и отправьте его ещё раз или нажмите /cancel.",
                    "⚠️ Crypto Pay couldn't verify the app. "
                    "Protected input remains active. Check the API token "
                    "in @CryptoBot and send it again, or use /cancel.",
                ),
            )
            return
        # Only the original generation may be cleared. Never erase a
        # different form started while the external API call was pending.
        try:
            await consume_button_flow(
                factory, actor_id=actor, expected_step="crypto_pay_token",
                generation=flow.generation, bot_scope_id=bot.id,
                discard=True,
            )
        except ValueError:
            pass  # A newer owner form replaced this one during verification.
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
