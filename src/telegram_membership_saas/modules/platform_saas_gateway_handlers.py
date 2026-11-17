"""Central-only @vimipay_bot handoff for platform-owned Crypto Pay invoices.

No merchant-bot token or app can receive ViMi Pay's own service fee.
The payment link is presented only AFTER the original invoice ID, payer,
RUB balance and provider invoice ID have been atomically bound in SQL.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.platform_saas_gateway_service import (
    GatewayLink,
    issue_platform_crypto_checkout,
    record_operator_verified_gateway_refund,
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

    @router.message(Command("saasrefund"))
    async def platform_verified_provider_refund(
        message: Message, bot: Bot,
    ) -> None:
        """Operator-only evidence intake, never a provider refund request.

        Actual funds must have been independently refunded in Lava.top or
        the operator's Crypto Pay app BEFORE this command is used. No new
        payment menu, duplicate button or unauthenticated webhook.
        """
        settings = get_settings()
        if (
            message.chat.type != "private"
            or message.from_user is None
            or message.from_user.id != message.chat.id
            or message.from_user.id != settings.platform_owner_telegram_id
            or settings.telegram_central_access_enabled
        ):
            return
        fields = (message.text or "").split()
        if len(fields) != 6 or fields[5] != "VERIFIED":
            await bot.send_message(
                message.chat.id,
                tr(
                    "↩️ Сначала проверьте фактический возврат в кабинете "
                    "Lava.top или Crypto Pay. Для записи в финансовый журнал:\n"
                    "/saasrefund crypto <invoice_id> <refund_id> 150 VERIFIED\n"
                    "Это НЕ отправляет деньги клиенту.",
                    "↩️ First verify the actual refund in your Lava.top or "
                    "Crypto Pay provider account. Then record the evidence:\n"
                    "/saasrefund crypto <invoice_id> <refund_id> 150 VERIFIED\n"
                    "This does NOT send money to the customer.",
                ),
            )
            return
        try:
            provider, provider_id, refund_id = fields[1:4]
            amount = Decimal(fields[4])
            result = await record_operator_verified_gateway_refund(
                factory, settings=settings, provider=provider,
                provider_invoice_id=provider_id,
                provider_refund_id=refund_id, amount_rub=amount,
            )
        except (InvalidOperation, ValueError):
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Неверные реквизиты возврата. Проверьте провайдера, "
                    "ID и точную сумму в ₽.",
                    "⚠️ Invalid refund evidence. Verify provider, IDs "
                    "and exact RUB amount.",
                ),
            )
            return
        except Exception:
            log.exception("Operator-verified platform provider refund not recorded")
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Возврат не записан. Проверьте исходный платёж, "
                    "сумму и журнал перед повторной попыткой.",
                    "⚠️ Refund was not recorded. Verify original charge, "
                    "amount and ledger before retrying.",
                ),
            )
            return
        await bot.send_message(
            message.chat.id,
            tr(
                "✅ Возврат зарегистрирован в финансовом журнале. "
                "Подтверждённый возврат: {amount} ₽, "
                "возврат кредита по счёту: {credit} ₽.",
                "✅ Verified refund recorded in the financial ledger. "
                "Actual refunded: {amount} RUB; invoice credit reversed: "
                "{credit} RUB.",
            ).format(
                amount=result["amount_rub"],
                credit=result["credit_reversed_rub"],
            ) if result["recorded"] else tr(
                "↩️ Этот возврат уже был зарегистрирован ранее.",
                "↩️ This refund was already recorded.",
            ),
        )

    return router
