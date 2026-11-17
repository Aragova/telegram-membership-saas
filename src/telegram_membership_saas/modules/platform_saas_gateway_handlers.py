"""Central-only @vimipay_bot handoff for platform-owned Crypto Pay invoices.

No merchant-bot token or app can receive ViMi Pay's own service fee.
The payment link is presented only AFTER the original invoice ID, payer,
RUB balance and provider invoice ID have been atomically bound in SQL.
"""
from __future__ import annotations

import logging
import re
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.platform_saas_gateway_service import (
    GatewayLink,
    issue_platform_crypto_checkout,
)
from telegram_membership_saas.modules.telegram_commerce.service import decode_uuid

log = logging.getLogger(__name__)

START_CRYPTO = re.compile(
    r"^/start(?:@[A-Za-z0-9_]{5,32})? sc_([A-Za-z0-9_-]{22})$"
)


async def send_platform_crypto_checkout(
    bot: Bot, factory: async_sessionmaker[AsyncSession], *,
    payer_telegram_id: int, invoice_id: UUID,
) -> GatewayLink:
    link = await issue_platform_crypto_checkout(
        factory, settings=get_settings(),
        payer_telegram_id=payer_telegram_id,
        central_bot_id=bot.id, invoice_id=invoice_id,
    )
    await bot.send_message(
        payer_telegram_id,
        tr(
            "💎 <b>Crypto Pay · счёт ViMi Pay</b>\n"
            "К оплате: <b>{amount:.2f} ₽</b>\n"
            "Зачисление производится автоматически после проверки платежа.",
            "💎 <b>Crypto Pay · ViMi Pay invoice</b>\n"
            "Amount: <b>{amount:.2f} RUB</b>\n"
            "Settlement follows automatic provider verification.",
        ).format(amount=link.amount_rub),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=tr("💎 Открыть оплату", "💎 Open payment"),
                url=link.invoice_url,
            ),
        ]]),
        parse_mode="HTML",
    )
    return link


def create_platform_saas_gateway_router(
    factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="platform-saas-central-gateways")

    @router.message(F.text.regexp(START_CRYPTO))
    async def open_crypto_from_original_bot(message: Message, bot: Bot) -> None:
        if (
            message.chat.type != "private" or message.from_user is None
            or message.chat.id != message.from_user.id or not message.text
        ):
            return
        match = START_CRYPTO.fullmatch(message.text)
        if match is None:
            return
        try:
            invoice_id = decode_uuid(match.group(1))
            await send_platform_crypto_checkout(
                bot, factory,
                payer_telegram_id=message.from_user.id,
                invoice_id=invoice_id,
            )
        except Exception:
            # No provider token, invoice ID, PII or provider error is echoed.
            log.warning("Central platform Crypto Pay checkout is unavailable")
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ 💎 Crypto Pay пока недоступен для этого счёта. "
                    "Средства не списаны. Вернитесь к способам оплаты.",
                    "⚠️ 💎 Crypto Pay is unavailable for this invoice. "
                    "No charge was made. Return to payment methods.",
                ),
            )

    return router
