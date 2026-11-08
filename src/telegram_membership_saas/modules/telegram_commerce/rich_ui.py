"""Opt-in Rich subscription offer cards; no changes to checkout or payment state.

Real buyer mode is disabled by default and scoped to explicit Telegram bot IDs.
Owner preview uses the same rendering with safe, owner-only callback buttons.
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.telegram_commerce.service import (
    format_amount,
    format_period,
    is_period_only_plan_name,
)
from telegram_membership_saas.modules.telegram_commerce.storefront import BuyerOffer

log = logging.getLogger(__name__)


def rich_subscription_enabled(bot_id: int) -> bool:
    """Explicit bot-ID allowlist; empty setting keeps all subscriptions classic."""
    settings = get_settings()
    if hasattr(settings, "rich_messages_enabled"):
        return settings.rich_messages_enabled
    configured = settings.rich_subscription_checkout_bot_ids
    return str(bot_id) in {
        part.strip() for part in configured.split(",") if part.strip()
    }


def rich_subscription_offer_html(
    offer: BuyerOffer, *, preview: bool = False,
) -> str:
    """Build a localized Rich table using the same checkout-eligible offer."""
    option = offer.option
    title = tr("💎 Оформление подписки", "💎 Subscription checkout")
    period = format_period(option.duration_value, option.duration_unit)
    amount = format_amount(option.amount, option.currency_precision)
    price = amount + " " + option.currency_code
    parts = [
        "<h3>" + title + "</h3>",
        "<table bordered compact>",
        "<tr><th>" + tr("📢 Канал", "📢 Channel")
        + "</th><td>" + escape(offer.resource_label) + "</td></tr>",
    ]
    if not is_period_only_plan_name(
        option.plan_name, option.duration_value, option.duration_unit,
    ):
        parts.append(
            "<tr><th>" + tr("🎟 Тариф", "🎟 Plan")
            + "</th><td>" + escape(option.plan_name) + "</td></tr>"
        )
    parts.extend([
        "<tr><th>" + tr("🗓️ Срок", "🗓️ Duration")
        + "</th><td>" + escape(period) + "</td></tr>",
        "<tr><th>" + tr("💰 Цена", "💰 Price")
        + "</th><td>" + escape(price) + "</td></tr>",
        "</table>",
    ])
    if preview:
        parts.append("<p><b>" + tr(
            "✨ Предпросмотр оформления тарифа. Оплата не создаётся.",
            "✨ Plan design preview. No payment is created.",
        ) + "</b></p>")
    else:
        parts.append("<p><b>" + tr(
            "👇 Как вам удобнее оплатить?",
            "👇 How would you like to pay?",
        ) + "</b></p>")
    return "\n".join(parts)


async def send_rich_subscription_offer(
    bot: Bot, *, chat_id: int, rich_html: str,
    reply_markup: InlineKeyboardMarkup, preview: bool = False,
    message_id: int | None = None,
) -> bool:
    """Send or edit a Rich subscription screen with the SAME callbacks.

    When opened by clicking a tariff, edit the original catalog message.
    This avoids stacking an extra block of old tariff/payment buttons.
    """
    if not preview and not rich_subscription_enabled(bot.id):
        return False
    try:
        from aiogram.types import InputRichMessage
    except ImportError:
        log.info("Rich subscription messages unsupported by installed aiogram")
        return False
    rich = InputRichMessage(html=rich_html)
    try:
        if message_id is not None:
            if "rich_message" not in EditMessageText.model_fields:
                return False
            await bot(EditMessageText(
                text=None, chat_id=chat_id, message_id=message_id,
                rich_message=rich, reply_markup=reply_markup,
            ))
        else:
            send = getattr(bot, "send_rich_message", None)
            if not callable(send):
                return False
            await send(
                chat_id=chat_id, rich_message=rich,
                reply_markup=reply_markup,
            )
        return True
    except TelegramBadRequest as exc:
        # The caller already treats an unchanged message as no-op.
        if "message is not modified" in str(exc).lower():
            return True
        log.warning("Rich subscription offer rejected for bot %s: %s", bot.id, exc)
        return False
    except TelegramNotFound as exc:
        if "method not found" not in str(exc).lower():
            raise
        log.warning("Rich subscription API missing for bot %s", bot.id)
        return False
