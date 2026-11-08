"""Opt-in rich file checkout with a safe legacy Telegram message fallback.

Only visuals change. Existing inline keyboard callback_data and purchase
services remain unchanged. No retries on transport failures to avoid doubles.
"""
from __future__ import annotations

import inspect
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import InlineKeyboardMarkup

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import classic_messages

log = logging.getLogger(__name__)


def rich_file_checkout_enabled(bot_id: int) -> bool:
    """Disabled by default. Allow only explicit connected Telegram bot IDs."""
    settings = get_settings()
    if hasattr(settings, "rich_messages_enabled"):
        return settings.rich_messages_enabled
    ids = settings.rich_file_checkout_bot_ids
    return str(bot_id) in {part.strip() for part in ids.split(",") if part.strip()}


def rich_file_card_html(
    *, product_name: str, description: str | None, price: str,
    file_count: int, preview: bool = False,
) -> str:
    """Rich HTML for supported Telegram clients, never interpolating raw user HTML."""
    title = tr("💎 Покупка файла", "💎 File purchase")
    label_name = tr("🏷 Название", "🏷 Name")
    label_price = tr("💰 Цена", "💰 Price")
    label_files = tr("📎 Файлов", "📎 Files")
    prompt = (
        tr("✨ Предпросмотр оформления товара", "✨ Product design preview")
        if preview else
        tr("👇 Выберите способ оплаты ниже", "👇 Select a payment method below")
    )
    parts = [
        "<h3>" + title + "</h3>",
        "<table bordered compact>",
        "<tr><th>" + label_name + "</th><td>" + escape(product_name) + "</td></tr>",
        "<tr><th>" + label_price + "</th><td>" + escape(price) + "</td></tr>",
        "<tr><th>" + label_files + "</th><td>" + str(file_count) + "</td></tr>",
        "</table>",
    ]
    if description:
        paragraphs = "".join(
            "<p>" + escape(line) + "</p>"
            for line in description.splitlines() if line.strip()
        )
        if paragraphs:
            parts.append(
                "<details><summary>"
                + tr("📝 Описание", "📝 Description")
                + "</summary>" + paragraphs + "</details>"
            )
    parts.append("<p><b>" + prompt + "</b></p>")
    return "\n".join(parts)


async def send_file_card(
    bot: Bot, *, chat_id: int, rich_html: str, legacy_html: str,
    reply_markup: InlineKeyboardMarkup,
    preview: bool = False,
) -> bool:
    """Return True if Rich succeeded, False for the unchanged legacy screen.

    Telegram may not recognize the method or rich HTML on older Bot API
    clients. Fall back only on API validation/method errors, not on transport
    failures (where a retry could result in duplicate messages).
    """
    if preview or rich_file_checkout_enabled(bot.id):
        try:
            from aiogram.types import InputRichMessage
        except ImportError:
            log.info("Rich Messages not supported by installed aiogram")
        else:
            send_rich = getattr(bot, "send_rich_message", None)
            if inspect.iscoroutinefunction(send_rich):
                try:
                    await send_rich(
                        chat_id=chat_id,
                        rich_message=InputRichMessage(html=rich_html),
                        reply_markup=reply_markup,
                    )
                    return True
                except TelegramBadRequest as exc:
                    log.warning(
                        "Rich file card API rejected formatting for bot %s: %s",
                        bot.id, exc,
                    )
                except TelegramNotFound as exc:
                    if "method not found" not in str(exc).lower():
                        raise
                    log.warning("sendRichMessage unsupported for bot %s", bot.id)

    with classic_messages():
        await bot.send_message(
            chat_id, legacy_html, parse_mode="HTML", reply_markup=reply_markup,
        )
    return False
