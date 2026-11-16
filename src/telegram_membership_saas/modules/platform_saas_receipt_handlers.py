"""Priority receipt ingress for real ViMi Pay platform invoices.

This router runs before tenant file editors and generic message handlers.
Telegram's original invoice prompt is durable, so a customer can submit a
photo or PDF after a supervisor restart without an in-memory receipt wizard.
"""
from __future__ import annotations

import logging
import re
from uuid import UUID

from aiogram import Bot, Router
from aiogram.filters import BaseFilter
from aiogram.types import (
    Message,
    RichBlockParagraph,
    RichBlockSectionHeading,
    RichText,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_proof_media import (
    ProofSizeUnavailableError,
    ProofTooLargeError,
    validate_platform_proof_size,
)
from telegram_membership_saas.modules.platform_saas_v2_payment_service import (
    saas_v2_submit_proof,
)

_LOG = logging.getLogger(__name__)
_PREFIX = "🧾 ViMi Pay · "
_NEW_HEADERS = ("🧾 ViMi Pay · Оплата счёта", "🧾 ViMi Pay · Invoice payment")
_PRIVATE_REF = re.compile(
    r"(?:^|\n)🔐 (?:Код заявки|Reference): "
    r"([0-9a-fA-F]{32}|[0-9a-fA-F-]{36})\s*$"
)



def _rich_text_prefix(value: object, *, max_chars: int = 160, depth: int = 0) -> str | None:
    """Read only bounded text nodes from Telegram's RichTextUnion.

    Rich bot messages do not populate Message.text. Their visible content
    arrives as RichMessage.blocks, potentially with a nested inline <code>
    UUID. Do not parse unknown objects or scrape arbitrary JSON/text dumps.
    """
    if depth > 8:
        return None
    if isinstance(value, str):
        return value[:max_chars]
    if isinstance(value, list):
        pieces: list[str] = []
        remaining = max_chars
        for element in value[:48]:
            if remaining <= 0:
                break
            part = _rich_text_prefix(
                element, max_chars=remaining, depth=depth + 1,
            )
            if part is None:
                return None
            pieces.append(part)
            remaining -= len(part)
        return "".join(pieces)
    if isinstance(value, RichText):
        return _rich_text_prefix(
            value.text, max_chars=max_chars, depth=depth + 1,
        ) if hasattr(value, "text") else None
    return None


def _invoice_prompt_text(prompt: Message) -> str | None:
    """Handle both classic HTML and already-issued Telegram Rich invoices."""
    plain = prompt.text or prompt.caption
    if plain:
        return plain
    rich = getattr(prompt, "rich_message", None)
    blocks = getattr(rich, "blocks", None)
    if not blocks:
        return None
    first = blocks[0]
    if not isinstance(first, (RichBlockParagraph, RichBlockSectionHeading)):
        return None
    # Older native Rich payment prompts embed the UUID in the first block.
    # New clean headers keep the private reference in the last paragraph.
    # Read bounded text nodes across blocks without trusting arbitrary JSON.
    collected: list[str] = []
    remaining = 4096
    for block in blocks[:48]:
        content = getattr(block, "text", None)
        if content is None:
            continue
        part = _rich_text_prefix(content, max_chars=remaining)
        if part is None:
            return None
        collected.append(part)
        remaining -= len(part)
        if remaining <= 0:
            break
    return "\n".join(collected) if collected else None


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
    text = _invoice_prompt_text(prompt) or ""
    if not text.startswith(_PREFIX):
        return None
    # Backward compatible: thousands of already-sent Rich/HTML instructions
    # carry the complete attempt UUID in their opening line.
    remainder = text[len(_PREFIX):].strip()
    first = remainder.split(maxsplit=1)[0] if remainder else ""
    try:
        return UUID(first)
    except (ValueError, AttributeError):
        pass
    # New HTML instructions have a human title; their original attempt ID is
    # kept in a Telegram spoiler at the very end, not displayed as a heading.
    # We still require the ORIGINAL reply_to_message from this exact bot.
    if not text.startswith(_NEW_HEADERS):
        return None
    matched = _PRIVATE_REF.search(text)
    if matched is None:
        return None
    try:
        return UUID(matched.group(1))
    except ValueError:
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
            file_size = message.photo[-1].file_size
        else:
            file_type, file_id = "document", message.document.file_id
            file_size = message.document.file_size
        try:
            await validate_platform_proof_size(
                bot, file_id=file_id, file_type=file_type,
                reported_size=file_size,
            )
        except ProofTooLargeError:
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Документ превышает 20 МиБ или фото превышает "
                    "10 МиБ. Отправьте меньший файл ответом на инструкцию "
                    "об оплате. Чек не зарегистрирован.",
                    "⚠️ Documents must not exceed 20 MiB and photos "
                    "10 MiB. Reply to the payment instruction with a "
                    "smaller file. Receipt was not registered.",
                ),
            )
            return
        except ProofSizeUnavailableError:
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Не удалось проверить размер файла в Telegram. "
                    "Повторите отправку меньшего чека. "
                    "Заявка пока не создана.",
                    "⚠️ Telegram file size cannot be verified. "
                    "Please retry with a smaller receipt. "
                    "No review request was created.",
                ),
            )
            return
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

        # The proof INSERT atomically enqueues one central-bot notification.
        # Do not try to notify the platform owner from each connected bot:
        # many operators have never /started that particular bot.


    return router
