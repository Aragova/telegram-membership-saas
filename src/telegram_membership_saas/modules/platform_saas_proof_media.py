"""Read a pending ViMi Pay bank proof through its *original* Telegram bot.

Telegram file_id is bot-scoped. The central bot cannot forward an unrelated
bot's file_id. Re-download with the original bot's encrypted credential and
re-upload the bytes only to the verified platform operator's central chat.
No receipt media is stored in the database or local filesystem.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import decrypt_token
from telegram_membership_saas.modules.platform_saas_v2_payment_service import (
    saas_pending_proof_source,
)

_MAX_PHOTO_BYTES = 10 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
_DOCUMENT_SUFFIXES = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".doc", ".docx",
})


async def reupload_proof_media(
    source_bot: Bot, central_bot: Bot, *, operator_id: int,
    source_file_id: str, file_type: str, attempt_id: UUID, caption: str,
) -> None:
    """Re-upload from original bot. Never pass its file_id to central Bot API."""
    if operator_id <= 0 or file_type not in ("photo", "document"):
        raise ValueError("Invalid platform proof recipient or media type")
    source = await source_bot.get_file(source_file_id)
    if not source.file_path:
        raise ValueError("Original proof file is unavailable")
    limit = (_MAX_PHOTO_BYTES if file_type == "photo"
             else _MAX_DOCUMENT_BYTES)
    if source.file_size is not None and source.file_size > limit:
        raise ValueError("Original proof exceeds Telegram transfer limit")
    stream = await source_bot.download_file(source.file_path)
    if not isinstance(stream, BytesIO):
        raise ValueError("Original proof could not be downloaded")
    data = stream.getvalue()
    if not data or len(data) > limit:
        raise ValueError("Invalid proof file size")

    if file_type == "photo":
        await central_bot.send_photo(
            operator_id, BufferedInputFile(
                data, filename=f"vimi-receipt-{attempt_id.hex[:8]}.jpg",
            ), caption=caption[:1024],
        )
    else:
        suffix = PurePosixPath(source.file_path).suffix.lower()
        if suffix not in _DOCUMENT_SUFFIXES:
            suffix = ".bin"
        await central_bot.send_document(
            operator_id, BufferedInputFile(
                data, filename=f"vimi-receipt-{attempt_id.hex[:8]}{suffix}",
            ), caption=caption[:1024],
        )


async def show_pending_platform_proof(
    factory: async_sessionmaker[AsyncSession], *,
    central_bot: Bot, operator_id: int, actor_id: UUID,
    attempt_id: UUID, settings: Settings, locale_is_english: bool = False,
) -> None:
    """Operator-scoped proof lookup; do not grant global tenant-token access."""
    proof = await saas_pending_proof_source(
        factory, actor_id=actor_id, actor_telegram_id=operator_id,
        attempt_id=attempt_id, settings=settings,
    )
    source_bot_id = int(proof["source_bot"])
    # The central bot can receive payment proofs too. It has no
    # BotInstance/project and must use its live, already-authenticated Bot.
    # Do not close the central transport after sending its own receipt.
    close_source = False
    if source_bot_id == central_bot.id:
        source_bot = central_bot
    else:
        key = settings.bot_token_encryption_key
        if key is None or not proof.get("source_project"):
            raise ValueError("Original bot credentials are unavailable")
        project_id = UUID(str(proof["source_project"]))
        async with system_project_session_scope(factory, project_id) as session:
            cipher = await session.scalar(
                select(BotInstance.token_ciphertext).where(
                    BotInstance.project_id == project_id,
                    BotInstance.telegram_bot_id == source_bot_id,
                    BotInstance.status == "registered",
                )
            )
        if not cipher:
            raise ValueError("Original bot is disconnected")
        source_bot = Bot(token=decrypt_token(cipher, key=key))
        close_source = True
    try:
        # Rotated or mismatched tokens must not read another bot's media.
        if source_bot.id != source_bot_id:
            raise ValueError("Original bot identity mismatch")
        caption = (
            ("🧾 ViMi Pay · Receipt for bank review\n"
             if locale_is_english else "🧾 ViMi Pay · Чек на проверку\n")
            + f"№ {attempt_id}\n"
            + ("Invoice: " if locale_is_english else "Счёт: ")
            + str(proof["invoice_id"]) + "\n"
            + ("Transfer: " if locale_is_english else "Перевод: ")
            + str(proof["amount"]) + " " + str(proof["currency"]) + "\n"
            + ("Original instructions: " if locale_is_english
               else "Реквизиты из заявки: ")
            + str(proof["instructions"])[:460] + "\n"
            + ("⚠️ Verify the funds in your bank before approval."
               if locale_is_english
               else "⚠️ Проверьте поступление в банке перед подтверждением.")
        )
        await reupload_proof_media(
            source_bot, central_bot, operator_id=operator_id,
            source_file_id=str(proof["source_file"]),
            file_type=str(proof["file_type"]),
            attempt_id=attempt_id, caption=caption,
        )
    finally:
        if close_source:
            await source_bot.session.close()
