"""Priority receipt ingress for real ViMi Pay platform invoices.

This router runs before tenant file editors and generic message handlers.
Telegram's original invoice prompt is durable, so a customer can submit a
photo or PDF after a supervisor restart without an in-memory receipt wizard.
"""
from __future__ import annotations

import logging
from uuid import UUID

from aiogram import Bot, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import BaseFilter
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_v2_payment_service import (
    saas_v2_submit_proof,
)

_LOG = logging.getLogger(__name__)
_PREFIX = "🧾 ViMi Pay · "


def receipt_attempt_id(message: Message, bot_id: int) -> UUID | None:
    """Parse ONLY media replies to an authentic prompt from this exact bot.

    Telegram delivers HTML <code> as plain text. Accept canonical UUID and
    compact UUID forms; never trust arbitrary captions or forwarded invoices.
    """
    if (
        message.from_user is None
        or message.chat.type != "private"
        or message.chat.id != message.from_user.id
        or not (message.photo or message.document)
    ):
        return None
    prompt = message.reply_to_message
    if (
        prompt is None or prompt.from_user is None
        or prompt.from_user.id != bot_id
    ):
        return None
    text = prompt.text or prompt.caption or ""
    if not text.startswith(_PREFIX):
        return None
    remainder = text[len(_PREFIX):].strip()
    if not remainder:
        return None
    token = remainder.split(maxsplit=1)[0]
    try:
        return UUID(token)
    except (ValueError, AttributeError):
        return None


class ViMiPlatformProofFilter(BaseFilter):
    async def __call__(self, message: Message, bot: Bot) -> bool:
        return receipt_attempt_id(message, bot.id) is not None


def create_saas_receipt_router(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID | None = None,
) -> Router:
    router = Router(name="vimi-saas-proof-ingress")

    @router.message(ViMiPlatformProofFilter())
    async def receive_platform_payment_proof(message: Message, bot: Bot) -> None:
        # This router is intentionally ahead of FileEditorFilter and
        # broadcasts; an active file editor must never steal a bank proof.
        attempt = receipt_attempt_id(message, bot.id)
        if attempt is None:
            return
        if message.photo:
            file_type, file_id = "photo", message.photo[-1].file_id
        else:
            file_type, file_id = "document", message.document.file_id
        try:
            actor = await ensure_telegram_actor(
                factory, telegram_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
            )
            await saas_v2_submit_proof(
                factory, actor_id=actor, attempt_id=attempt,
                project_id=project_id, bot_id=bot.id,
                chat_id=message.chat.id, message_id=message.message_id,
                file_id=file_id, file_type=file_type,
            )
        except (ValueError, PermissionError):
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Этот чек не относится к вашему действующему счёту. "
                    "Откройте 🧾 Счёт и повторите оплату.",
                    "⚠️ This receipt does not match an active invoice. "
                    "Open 🧾 Bill and try again.",
                ),
            )
            return
        except Exception:
            _LOG.warning("ViMi Pay receipt registration failed")
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Чек не удалось сохранить. Повторите отправку.",
                    "⚠️ The receipt could not be recorded. Please retry.",
                ),
            )
            return

        await bot.send_message(
            message.chat.id,
            tr(
                "✅ Чек зарегистрирован. Оплата будет зачтена только после "
                "подтверждения поступления администратором ViMi Pay.",
                "✅ Receipt recorded. The platform operator must verify "
                "actual bank funds before this payment is credited.",
            ),
        )

        # Proof metadata is durable and belongs to the original source bot.
        # Owner push is best-effort: the central operator can always open
        # the pending queue and use the on-demand proof viewer (PR #153).
        operator_tg = get_settings().platform_owner_telegram_id
        if operator_tg and operator_tg != message.from_user.id:
            try:
                caption = (
                    "🧾 ViMi Pay · " + attempt.hex + "\n"
                    + tr(
                        "Платёжный чек на проверку. Сумму и реквизиты "
                        "сверьте с карточкой в 👑 Финансы.",
                        "Payment proof for review. Match bank details "
                        "and amount against 👑 Finances.",
                    )
                )
                if file_type == "photo":
                    await bot.send_photo(operator_tg, file_id, caption=caption)
                else:
                    await bot.send_document(operator_tg, file_id, caption=caption)
            except AiogramError:
                _LOG.warning(
                    "ViMi Pay operator receipt push unavailable; "
                    "pending receipt is persisted"
                )


    return router
