"""Notify the paying channel owner of each real ViMi Pay bank decision.

The immutable decision creates a database outbox row. This routine runs in
the EXISTING central monitor task, but delivers from the ORIGINAL receiving
bot so the account owner gets the reply in the same private chat.
"""
from __future__ import annotations

import inspect
import logging
from html import escape
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.localization import recipient_language, tr
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import decrypt_token
from telegram_membership_saas.rich_messages import (
    InputRichMessage,
    classic_messages,
    rich_enabled,
)
from telegram_membership_saas.tenant_context import TenantContext

log = logging.getLogger(__name__)


async def _claim(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
) -> dict | None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        result = (await session.execute(text(
            "SELECT public.platform_saas_review_result_claim() AS data"
        ).columns(data=JSONB))).scalar_one()
    if result is not None and (
        not isinstance(result, dict)
        or any(result.get(key) is None for key in (
            "id", "decision", "generation", "source_bot", "source_chat",
            "amount", "currency",
        ))
    ):
        raise ValueError("Invalid ViMi Pay review-result claim")
    return result


async def _finish(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    attempt_id: UUID, generation: int, sent: bool,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        await session.execute(text(
            "SELECT public.platform_saas_review_result_finish("
            ":attempt,:generation,:sent)"
        ), {"attempt": attempt_id, "generation": generation, "sent": sent})


def _texts(item: dict) -> tuple[str, str]:
    """Rich/card and classic fallback share the same reviewed bank outcome."""
    approved = item["decision"] == "approved"
    if item["decision"] not in ("approved", "rejected"):
        raise ValueError("Unknown bank review result")
    heading = (
        tr("✅ ViMi Pay · Оплата подтверждена",
           "✅ ViMi Pay · Payment confirmed")
        if approved else
        tr("❌ ViMi Pay · Чек отклонён", "❌ ViMi Pay · Receipt rejected")
    )
    amount = escape(str(item["amount"]) + " " + str(item["currency"]))
    status = (
        tr("Оплачено после проверки банка", "Verified bank payment")
        if approved else
        tr("Не оплачено", "Unpaid")
    )
    reason = escape(str(item.get("reason") or "").strip())
    info = (
        tr("Оплата подтверждена ViMi Pay. Актуальный счёт можно "
           "открыть кнопкой ниже.", "ViMi Pay verified the funds. "
           "Open the current invoice using the button below.")
        if approved else
        tr("Чек не принят, сумма не зачтена. Счёт остаётся "
           "неоплаченным. Нажмите «К счёту» ниже, чтобы "
           "повторить оплату.",
           "Your receipt was not accepted and no funds were credited. "
           "The invoice remains unpaid. Use the Invoice button below "
           "to try again.")
    )
    reason_row = (
        "<tr><th>" + tr("Причина", "Reason") + "</th><td>"
        + reason + "</td></tr>"
        if not approved else ""
    )
    rich = (
        "<h3>" + heading + "</h3>"
        "<table bordered compact>"
        "<tr><th>" + tr("Перевод", "Transfer") + "</th><td>"
        + amount + "</td></tr>"
        "<tr><th>" + tr("Статус", "Status") + "</th><td>"
        + status + "</td></tr>"
        + reason_row + "</table><p>" + info + "</p>"
    )
    classic = (
        "<b>" + heading + "</b>\n\n"
        + tr("Перевод: ", "Transfer: ") + "<b>" + amount + "</b>\n"
        + tr("Статус: ", "Status: ") + status
        + (("\n" + tr("Причина: ", "Reason: ") + reason)
           if not approved else "")
        + "\n\n" + info
    )
    return rich, classic


def _result_navigation() -> InlineKeyboardMarkup:
    """Re-use the actual admin bot's existing invoice screen, not a new menu.

    own:saasinvoices:0 is already handled inside the owner setup router for
    BOTH paid and unpaid invoices; the resulting screen has its own menu
    navigation. Never expose a technical invoice UUID in callback text.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=tr("⬅️ К счёту", "⬅️ Invoice"),
            callback_data="own:saasinvoices:0",
        ),
    ]])


async def _send_result(bot: Bot, chat_id: int, item: dict) -> None:
    rich, classic = _texts(item)
    reply_markup = _result_navigation()
    sender = getattr(bot, "send_rich_message", None)
    if (
        rich_enabled() and InputRichMessage is not None
        and inspect.iscoroutinefunction(sender)
        and len(rich.encode("utf-8")) < 32768
    ):
        try:
            await sender(
                chat_id=chat_id, rich_message=InputRichMessage(html=rich),
                reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest as exc:
            if not any(word in str(exc).lower() for word in (
                "rich", "can't parse", "unsupported", "invalid html",
            )):
                raise
        except TelegramNotFound as exc:
            if "method not found" not in str(exc).lower():
                raise
    with classic_messages():
        await bot.send_message(
            chat_id, classic, parse_mode="HTML", reply_markup=reply_markup,
        )


async def _send_using_source_bot(
    factory: async_sessionmaker[AsyncSession], *,
    central_bot: Bot, settings: Settings, item: dict,
) -> None:
    source_id, chat_id = int(item["source_bot"]), int(item["source_chat"])
    if source_id <= 0 or chat_id <= 0:
        raise ValueError("Missing original ViMi Pay bot/chat")
    close_source = False
    if source_id == central_bot.id:
        bot = central_bot
    else:
        project = item.get("source_project")
        key = settings.bot_token_encryption_key
        if project is None or key is None:
            raise ValueError("Original connected bot is unavailable")
        project_id = UUID(str(project))
        async with system_project_session_scope(factory, project_id) as session:
            cipher = await session.scalar(select(
                BotInstance.token_ciphertext
            ).where(
                BotInstance.project_id == project_id,
                BotInstance.telegram_bot_id == source_id,
                BotInstance.status == "registered",
            ))
        if not cipher:
            raise ValueError("Original connected bot is unavailable")
        bot = Bot(token=decrypt_token(cipher, key=key))
        close_source = True
    try:
        if bot.id != source_id:
            raise ValueError("Original ViMi Pay bot has changed")
        async with recipient_language(factory, chat_id):
            await _send_result(bot, chat_id, item)
    finally:
        if close_source:
            await bot.session.close()


async def deliver_one_saas_review_result(
    factory: async_sessionmaker[AsyncSession], *,
    central_bot: Bot, settings: Settings, actor_id: UUID,
) -> bool:
    """Process one durable result. Do not roll back bank decisions on failure."""
    item = await _claim(factory, actor_id=actor_id)
    if item is None:
        return False
    attempt_id = UUID(str(item["id"]))
    generation = int(item["generation"])
    sent = False
    try:
        await _send_using_source_bot(
            factory, central_bot=central_bot, settings=settings, item=item,
        )
        sent = True
    except Exception:
        # No financial reference, receipt media or bot token in server logs.
        log.warning("ViMi Pay review-result delivery deferred")
    finally:
        await _finish(
            factory, actor_id=actor_id, attempt_id=attempt_id,
            generation=generation, sent=sent,
        )
    return True
