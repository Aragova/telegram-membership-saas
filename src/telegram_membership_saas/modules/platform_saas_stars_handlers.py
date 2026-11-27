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
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.platform_saas_crypto_client import (
    verify_platform_crypto_app,
)
from telegram_membership_saas.modules.platform_saas_stars_service import (
    configure_platform_stars_usd_policy,
    confirm_platform_stars_charge,
    create_platform_stars_order,
    platform_stars_precheckout,
    platform_stars_usd_policy,
    record_platform_stars_refund,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    decode_uuid,
    encode_uuid,
)
from telegram_membership_saas.stars_checkout_ui import (
    log_stars_checkout_failure,
    stars_checkout_rows,
    stars_topup_guidance,
)

LOG = logging.getLogger(__name__)

START_PATTERN = re.compile(r"^/start(?:@[A-Za-z0-9_]{5,32})? sa_([A-Za-z0-9_-]{22})$")


def stars_payment_back(invoice_id: UUID, page_code: str = "0") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=tr("⬅️ Назад", "⬅️ Back"),
        callback_data="own:spm:" + encode_uuid(invoice_id) + ":RUB:" + page_code,
        style="primary",
    )


async def build_platform_stars_checkout(
    bot: Bot,
    factory: async_sessionmaker[AsyncSession],
    *, payer_telegram_id: int, invoice_id: UUID, page_code: str = "0",
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """One frozen quote, one native payment link; no charge on screen entry."""
    order = await create_platform_stars_order(
        factory, invoice_id=invoice_id,
        payer_telegram_id=payer_telegram_id, central_bot_id=bot.id,
    )
    amount = int(order["stars_amount"])
    link = await bot.create_invoice_link(
        title=tr("ViMi Pay · Оплата счёта", "ViMi Pay · Invoice payment"),
        description=tr(
            "Оплата счёта ViMi Pay.\n\nК оплате: {stars} ⭐",
            "ViMi Pay invoice payment.\n\nTotal: {stars} ⭐",
        ).format(stars=amount),
        payload=order["payload"],
        currency="XTR",
        prices=[LabeledPrice(label=tr("⭐ Telegram Stars", "⭐ Telegram Stars"), amount=amount)],
    )
    username = order.get("bot_username")
    bot_label = (
        "🤖 <b>@" + username + "</b>"
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
        else tr("🤖 Счёт бота", "🤖 Bot invoice")
    )
    html = (
        tr("✦ <b>ViMi Pay</b>\n⭐ <b>TELEGRAM STARS · ОПЛАТА СЧЁТА</b>",
           "✦ <b>ViMi Pay</b>\n⭐ <b>TELEGRAM STARS · INVOICE PAYMENT</b>")
        + "\n\n" + bot_label
        + "\n\n" + tr("Итого к оплате", "Total due")
        + "\n<b>" + str(amount) + " ⭐</b>\n\n"
        + stars_topup_guidance()
        + "\n\n" + tr(
            "Сумма действует 20 минут. Если срок истёк, "
            "вернитесь назад и снова выберите Telegram Stars.",
            "The quote is valid for 20 minutes. If it expires, "
            "go back and select Telegram Stars again.",
        )
    )
    rows = stars_checkout_rows(
        amount=amount, link=link, back=stars_payment_back(invoice_id, page_code),
    )
    return html, rows


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
        invoice_id = None
        try:
            invoice_id = decode_uuid(match.group(1))
            html, rows = await build_platform_stars_checkout(
                bot, factory,
                payer_telegram_id=message.from_user.id, invoice_id=invoice_id,
            )
            await bot.send_message(
                message.chat.id, html, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except Exception as exc:
            log_stars_checkout_failure(
                LOG, "Central platform Stars deep-link checkout unavailable", exc,
            )
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ ⭐ Оплата счёта пока недоступна. Проверьте "
                    "выставленный счёт или обратитесь в поддержку.",
                    "⚠️ ⭐ Invoice payment is currently unavailable. "
                    "Check your issued invoice or contact support.",
                ),
                reply_markup=(
                    InlineKeyboardMarkup(inline_keyboard=[[stars_payment_back(invoice_id)]])
                    if invoice_id is not None else None
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

    @router.message(Command("saasrails"))
    async def platform_rails_readiness(message: Message, bot: Bot) -> None:
        """Only the platform owner may inspect read-only rail readiness."""
        settings = get_settings()
        actor = message.from_user
        if (
            settings.telegram_central_access_enabled
            or settings.platform_owner_telegram_id is None
            or actor is None
            or message.chat.type != "private" or message.chat.id != actor.id
            or actor.id != settings.platform_owner_telegram_id
        ):
            return

        def has_secret(value: object) -> bool:
            return bool(value is not None and value.get_secret_value().strip())

        lava_ready = (
            has_secret(settings.saas_lava_top_api_key)
            and settings.saas_lava_top_offer_id is not None
        )
        try:
            policy = await platform_stars_usd_policy(
                factory, operator_telegram_id=actor.id,
            )
        except Exception:
            LOG.warning("Platform USD Stars quote readiness unavailable")
            stars_status = tr("⚠️ Не удалось проверить USD-котировку",
                              "⚠️ USD quote check unavailable")
        else:
            quote = (
                "$5 → " + str(policy["example_stars"]) + " ⭐ ("
                + str(policy["usd_per_star"]) + " USD/⭐, "
                + str(policy["withdrawal_reserve_pct"]) + "% "
                + tr("резерв на вывод)", "withdrawal reserve)")
            )
            stars_status = (
                "✅ " + quote if policy["enabled"]
                else tr("⚪ Выключено. Включить: /saasstarsrate 0.013 5. ",
                        "⚪ Disabled. Enable: /saasstarsrate 0.013 5. ") + quote
            )

        crypto_ready = has_secret(settings.saas_crypto_pay_api_token)
        if (
            crypto_ready and settings.environment == "production"
            and settings.saas_crypto_pay_testnet
        ):
            crypto_status = tr("⛔ Testnet запрещён в production",
                               "⛔ Testnet is prohibited in production")
        elif crypto_ready:
            try:
                verified = await verify_platform_crypto_app(settings)
            except Exception:
                # The adapter sanitizes API credentials before raising.
                # Keep provider details out of Telegram; retain traceback
                # in private application logs for operator diagnostics.
                LOG.exception("Platform Crypto Pay getMe readiness failed")
                crypto_status = tr(
                    "⚠️ Проверка Crypto Pay временно недоступна",
                    "⚠️ Crypto Pay readiness check is temporarily unavailable",
                )
            else:
                crypto_status = (
                    tr("✅ API-токен подтверждён Crypto Pay",
                       "✅ Crypto Pay confirmed the API token")
                    if verified else tr(
                        "⚠️ Токен задан, но провайдер не подтвердил приложение",
                        "⚠️ Token configured; provider app identity not confirmed",
                    )
                )
            if settings.saas_crypto_pay_testnet:
                crypto_status += tr(" (тестовая сеть)", " (testnet)")
        else:
            crypto_status = tr(
                "⚪ Нет отдельного API-токена платформы",
                "⚪ Separate platform API token is missing",
            )

        lava_status = (
            tr("✅ API-ключ и оффер заданы", "✅ API key and offer configured")
            if lava_ready else tr(
                "⚪ Нужны API-ключ и оффер",
                "⚪ API key and offer are needed",
            )
        )
        await bot.send_message(
            actor.id,
            tr("🔎 <b>Готовность оплаты счетов ViMi Pay</b>",
               "🔎 <b>ViMi Pay invoice rail readiness</b>")
            + "\n\n💳 Lava.top: " + lava_status
            + "\n⭐ Telegram Stars: " + stars_status
            + "\n💎 Crypto Pay: " + crypto_status
            + "\n\n"
            + tr(
                "Проверка не создаёт счета и не подтверждает платежи.",
                "This does not create invoices or confirm any payment.",
            ),
            parse_mode="HTML",
        )

    @router.message(Command("saasstarsrate"))
    async def configure_platform_stars_rate(
        message: Message, bot: Bot,
    ) -> None:
        """Set a USD payout reward and withdrawal reserve; never RUB/Star."""
        settings = get_settings()
        if (
            settings.telegram_central_access_enabled
            or settings.platform_owner_telegram_id is None
            or message.chat.type != "private"
            or message.from_user is None
            or message.from_user.id != message.chat.id
            or message.from_user.id != settings.platform_owner_telegram_id
        ):
            return
        args = (message.text or "").split()
        try:
            if len(args) == 1:
                policy = await platform_stars_usd_policy(
                    factory, operator_telegram_id=message.from_user.id,
                )
            elif len(args) == 2 and args[1].lower() == "off":
                current = await platform_stars_usd_policy(
                    factory, operator_telegram_id=message.from_user.id,
                )
                policy = await configure_platform_stars_usd_policy(
                    factory, operator_telegram_id=message.from_user.id,
                    usd_per_star=Decimal(current["usd_per_star"]),
                    reserve_percent=Decimal(current["withdrawal_reserve_pct"]),
                    enabled=False,
                )
            elif len(args) == 3:
                policy = await configure_platform_stars_usd_policy(
                    factory, operator_telegram_id=message.from_user.id,
                    usd_per_star=Decimal(args[1].replace(",", ".")),
                    reserve_percent=Decimal(args[2].replace(",", ".")),
                    enabled=True,
                )
            else:
                raise ValueError("Expected USD reward and reserve")
        except (InvalidOperation, ValueError):
            await bot.send_message(
                message.chat.id,
                tr("Укажите USD за ⭐ и резерв вывода в %: "
                   "/saasstarsrate 0.013 5. Выключить: /saasstarsrate off",
                   "Enter USD per ⭐ and withdrawal reserve %: "
                   "/saasstarsrate 0.013 5. Disable: /saasstarsrate off"),
            )
            return
        except Exception:
            LOG.warning("Platform USD Stars policy unavailable")
            await bot.send_message(
                message.chat.id,
                tr("⚠️ USD-котировка Stars сейчас недоступна.",
                   "⚠️ Stars USD quotation is unavailable."),
            )
            return
        await bot.send_message(
            message.chat.id,
            ("✅ " if policy["enabled"] else "⏸️ ")
            + tr("USD-котировка ⭐ Telegram Stars: ",
                 "⭐ Telegram Stars USD quote: ")
            + "$5 → " + str(policy["example_stars"]) + " ⭐\n"
            + str(policy["usd_per_star"]) + " USD/⭐, "
            + str(policy["withdrawal_reserve_pct"]) + "% "
            + tr("резерв на вывод. Только новые счета.",
                 "withdrawal reserve. New checkout quotes only."),
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
