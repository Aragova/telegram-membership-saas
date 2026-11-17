"""CENTRAL @vimipay_bot Stars checkout and platform invoice settlement.

Never mount this router on a tenant's customer bot. The connected-bot invoice
screen sends a private deep link here; the central bot alone invoices and
receives Telegram-authenticated successful_payment/refunded_payment updates.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.platform_saas_stars_service import (
    confirm_platform_stars_charge,
    create_platform_stars_order,
    platform_stars_precheckout,
    platform_stars_rate,
    record_platform_stars_refund,
    set_platform_stars_rate,
)
from telegram_membership_saas.modules.telegram_commerce.service import decode_uuid

LOG = logging.getLogger(__name__)

START_PATTERN = re.compile(r"^/start(?:@[A-Za-z0-9_]{5,32})? sa_([A-Za-z0-9_-]{22})$")


async def send_platform_stars_invoice(
    bot: Bot,
    factory: async_sessionmaker[AsyncSession],
    *, payer_telegram_id: int, invoice_id: UUID,
) -> None:
    """Create one immutable quote, then send it from the central bot."""
    order = await create_platform_stars_order(
        factory, invoice_id=invoice_id,
        payer_telegram_id=payer_telegram_id, central_bot_id=bot.id,
    )
    amount = int(order["stars_amount"])
    await bot.send_invoice(
        chat_id=payer_telegram_id,
        title=tr("ViMi Pay · обслуживание", "ViMi Pay · service"),
        description=tr(
            "Оплата выставленного счёта за обслуживание бота.",
            "Payment of the issued bot service invoice.",
        ),
        payload=order["payload"],
        currency="XTR",  # Telegram Bot API's Stars currency wire value.
        prices=[LabeledPrice(
            label=tr("⭐ Telegram Stars", "⭐ Telegram Stars"),
            amount=amount,
        )],
    )


def create_platform_saas_stars_router(
    factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="platform-saas-central-stars")

    @router.message(F.text.regexp(START_PATTERN))
    async def open_platform_stars_from_deeplink(
        message: Message, bot: Bot,
    ) -> None:
        if (
            message.chat.type != "private" or message.from_user is None
            or message.from_user.id != message.chat.id or not message.text
        ):
            return
        match = START_PATTERN.fullmatch(message.text)
        if match is None:
            return
        try:
            invoice_id = decode_uuid(match.group(1))
            await send_platform_stars_invoice(
                bot, factory,
                payer_telegram_id=message.from_user.id, invoice_id=invoice_id,
            )
        except Exception:
            # No bill identifiers or details in the error. Current owner
            # authorization, rate availability and remaining debt are SQL.
            LOG.warning("Central platform Stars deep-link checkout unavailable")
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ ⭐ Оплата счёта пока недоступна. Проверьте "
                    "выставленный счёт или обратитесь в поддержку.",
                    "⚠️ ⭐ Invoice payment is currently unavailable. "
                    "Check your issued invoice or contact support.",
                ),
            )

    @router.pre_checkout_query(F.invoice_payload.startswith("vps:"))
    async def platform_stars_pre_checkout(
        query: PreCheckoutQuery, bot: Bot,
    ) -> None:
        try:
            allowed = await platform_stars_precheckout(
                factory, payer_telegram_id=query.from_user.id,
                central_bot_id=bot.id, payload=query.invoice_payload,
                currency=query.currency, stars_amount=query.total_amount,
            )
        except Exception:
            LOG.exception("Central platform Stars precheckout check failed")
            allowed = False
        await bot.answer_pre_checkout_query(
            pre_checkout_query_id=query.id, ok=allowed,
            error_message=None if allowed else tr(
                "Счёт изменился или уже оплачен. Откройте его заново.",
                "The invoice changed or was already paid. Open it again.",
            ),
        )

    @router.message(F.successful_payment.invoice_payload.startswith("vps:"))
    async def platform_stars_success(
        message: Message, bot: Bot,
    ) -> None:
        payment = message.successful_payment
        if (
            message.chat.type != "private" or message.from_user is None
            or message.from_user.id != message.chat.id
            or payment is None
        ):
            return
        try:
            outcome = await confirm_platform_stars_charge(
                factory, payer_telegram_id=message.from_user.id,
                central_bot_id=bot.id, payload=payment.invoice_payload,
                currency=payment.currency, stars_amount=payment.total_amount,
                charge_id=payment.telegram_payment_charge_id,
                paid_at=message.date,
            )
        except Exception:
            # Telegram has ALREADY debited the user's Stars.
            # Never tell them to pay again or falsely claim fulfillment.
            LOG.exception("CRITICAL: paid ViMi Pay Stars pending DB reconciliation")
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Telegram подтвердил списание ⭐ Telegram Stars, "
                    "но учёт пока не завершён. Не платите повторно. "
                    "Обратитесь в /paysupport.",
                    "⚠️ Telegram charged your ⭐ Telegram Stars but "
                    "reconciliation is pending. Do not pay again. "
                    "Contact /paysupport.",
                ),
            )
            return
        if outcome.get("duplicate"):
            return
        if not outcome["applied"]:
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ ⭐ Telegram Stars списаны, но зачисление на счёт "
                    "невозможно из-за изменения суммы или владельца. "
                    "Повторно не платите. Обратитесь в /paysupport для возврата.",
                    "⚠️ ⭐ Telegram Stars were charged but cannot be "
                    "allocated because the invoice or owner changed. "
                    "Do not pay again. Contact /paysupport for a refund.",
                ),
            )
            return
        await bot.send_message(
            message.chat.id,
            tr(
                "✅ Счёт ViMi Pay оплачен через ⭐ Telegram Stars. "
                "Обслуживание бота продлено автоматически.",
                "✅ ViMi Pay invoice paid with ⭐ Telegram Stars. "
                "Your bot service has been renewed automatically.",
            ),
        )

    @router.message(F.refunded_payment.invoice_payload.startswith("vps:"))
    async def platform_stars_refunded(
        message: Message, bot: Bot,
    ) -> None:
        refund = message.refunded_payment
        if (
            message.chat.type != "private" or message.from_user is None
            or message.from_user.id != message.chat.id or refund is None
        ):
            return
        try:
            created = await record_platform_stars_refund(
                factory, payer_telegram_id=message.from_user.id,
                central_bot_id=bot.id, payload=refund.invoice_payload,
                currency=refund.currency, stars_amount=refund.total_amount,
                charge_id=refund.telegram_payment_charge_id,
                refunded_at=message.date,
            )
        except Exception:
            LOG.exception("CRITICAL: ViMi Pay Stars refund evidence not persisted")
            return
        if created:
            await bot.send_message(
                message.chat.id,
                tr(
                    "↩️ Возврат ⭐ Telegram Stars зарегистрирован. "
                    "Счёт снова может требовать оплаты; откройте /billing.",
                    "↩️ ⭐ Telegram Stars refund recorded. "
                    "The invoice may be due again; open /billing.",
                ),
            )

    @router.message(Command("saasstarsrate"))
    async def configure_platform_stars_rate(
        message: Message, bot: Bot,
    ) -> None:
        if (
            message.chat.type != "private" or message.from_user is None
            or message.from_user.id != message.chat.id
        ):
            return
        args = (message.text or "").split(maxsplit=1)
        try:
            if len(args) == 2:
                rate = Decimal(args[1].replace(",", "."))
                value = await set_platform_stars_rate(
                    factory, operator_telegram_id=message.from_user.id,
                    rub_per_star=rate,
                )
            else:
                value = await platform_stars_rate(
                    factory, operator_telegram_id=message.from_user.id,
                )
        except (InvalidOperation, ValueError):
            await bot.send_message(
                message.chat.id,
                tr(
                    "Укажите проверенный доход платформы в ₽ за 1 ⭐: "
                    "/saasstarsrate 2.50",
                    "Enter the verified platform RUB payout per 1 ⭐: "
                    "/saasstarsrate 2.50",
                ),
            )
            return
        except PermissionError:
            # Never reveal the operator's rate to ordinary merchant accounts.
            return
        except Exception:
            LOG.warning("Platform Stars operator pricing unavailable")
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Курс ⭐ Telegram Stars пока недоступен.",
                    "⚠️ ⭐ Telegram Stars payout rate is unavailable.",
                ),
            )
            return
        if value is None:
            await bot.send_message(
                message.chat.id,
                tr(
                    "⭐ Курс выплат ещё не задан. Оплата Stars закрыта.",
                    "⭐ Payout rate not set yet. Stars checkout is disabled.",
                ),
            )
        else:
            await bot.send_message(
                message.chat.id,
                tr("⭐ Курс выплат платформы: ", "⭐ Platform payout quote: ")
                + f"{value} ₽ / ⭐",
            )

    @router.message(Command("paysupport"))
    async def platform_stars_pay_support(
        message: Message, bot: Bot,
    ) -> None:
        if message.chat.type != "private":
            return
        await bot.send_message(
            message.chat.id,
            tr(
                "🛟 Поддержка платежей ViMi Pay. Если Telegram списал "
                "⭐ Telegram Stars, но счёт не закрылся, не оплачивайте "
                "повторно. Напишите владельцу платформы через /support.",
                "🛟 ViMi Pay payment support. If Telegram debited "
                "⭐ Telegram Stars but the invoice is not settled, "
                "do not pay again. Contact the platform via /support.",
            ),
        )

    return router
