"""Private central-bot Lava.top checkout, with short-lived payer email replies.

The merchant's bot never sees payer email or receives the platform's SaaS
funds. A prompt is merely a request for an email, NOT an accepted payment.
Only the existing SQL reservation, authenticated Lava GET reconciliation and
exact-once ledger can mark a ViMi Pay service invoice paid.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter
from aiogram.types import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_gateway_service import (
    issue_platform_lava_checkout,
)
from telegram_membership_saas.modules.platform_saas_lava_client import (
    LAVA_LIMITS,
    LavaTopError,
    validate_lava_customer_email,
)
from telegram_membership_saas.modules.platform_saas_v2_payment_service import (
    saas_v2_invoice_balance,
)
from telegram_membership_saas.modules.telegram_commerce.service import decode_uuid

LOG = logging.getLogger(__name__)
START_LAVA = re.compile(
    r"^/start(?:@[A-Za-z0-9_]{5,32})? sl_([A-Za-z0-9_-]{22})$"
)
EMAIL_REPLY_SECONDS = 600
MAX_EMAIL_PROMPTS = 256


@dataclass(frozen=True, slots=True)
class _PendingEmail:
    invoice_id: UUID
    central_bot_id: int
    expires_at: float


# Central process only, no cross-tenant or persistent email storage. A reboot
# loses the *unpaid prompt*, not the original invoice or provider-bound order.
_pending_lava: dict[tuple[int, int], _PendingEmail] = {}


def _prune_expired(now: float) -> None:
    for key, request in tuple(_pending_lava.items()):
        if now >= request.expires_at:
            _pending_lava.pop(key, None)


async def send_platform_lava_email_prompt(
    bot: Bot,
    factory: async_sessionmaker[AsyncSession],
    *, payer_telegram_id: int, invoice_id: UUID,
) -> None:
    """Validate the real owner's unpaid invoice BEFORE collecting any email."""
    settings = get_settings()
    if (
        settings.telegram_central_access_enabled
        or not isinstance(bot.id, int) or bot.id <= 0
        or payer_telegram_id <= 0
        or settings.saas_lava_top_api_key is None
        or settings.saas_lava_top_offer_id is None
    ):
        raise LavaTopError("Central Lava.top invoice issuing is not configured")
    actor_id = await ensure_telegram_actor(
        factory, telegram_id=payer_telegram_id,
        username=None, first_name=None,
    )
    balance = await saas_v2_invoice_balance(
        factory, actor_id=actor_id, invoice_id=invoice_id,
        project_id=None,
    )
    remaining = Decimal(str(balance["remaining_rub"]))
    if (
        not remaining.is_finite()
        or remaining < LAVA_LIMITS["RUB"][0]
        or remaining > LAVA_LIMITS["RUB"][1]
        or remaining != remaining.quantize(Decimal("0.01"))
    ):
        raise LavaTopError("Invoice cannot be settled via Lava.top")

    now = monotonic()
    _prune_expired(now)
    # Only the latest reply prompt per payer is valid. A stale reply cannot
    # unexpectedly initiate payment of a different invoice.
    for key in tuple(_pending_lava):
        if key[0] == payer_telegram_id:
            _pending_lava.pop(key, None)
    if len(_pending_lava) >= MAX_EMAIL_PROMPTS:
        raise LavaTopError("Lava.top email prompt capacity reached")

    prompt = await bot.send_message(
        payer_telegram_id,
        tr(
            "💳 <b>Lava.top · оплата ViMi Pay</b>\n"
            "К оплате: <b>{amount:.2f} ₽</b>\n\n"
            "Ответьте на это сообщение адресом email для выставления "
            "счёта. Адрес передаётся Lava.top, а не владельцу канала. "
            "Это ещё не платёж.",
            "💳 <b>Lava.top · ViMi Pay payment</b>\n"
            "Amount: <b>{amount:.2f} RUB</b>\n\n"
            "Reply to this message with the email address for checkout. "
            "The address is sent to Lava.top, not to the channel owner. "
            "No payment is made yet.",
        ).format(amount=remaining),
        parse_mode="HTML",
        reply_markup=ForceReply(
            selective=True, input_field_placeholder="name@example.com",
        ),
    )
    _pending_lava[(payer_telegram_id, prompt.message_id)] = _PendingEmail(
        invoice_id=invoice_id, central_bot_id=bot.id,
        expires_at=monotonic() + EMAIL_REPLY_SECONDS,
    )


class _LavaEmailReplyFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        reply = message.reply_to_message
        return bool(
            message.chat.type == "private"
            and message.from_user is not None
            and message.from_user.id == message.chat.id
            and reply is not None
            and (message.from_user.id, reply.message_id) in _pending_lava
        )


def create_platform_saas_lava_router(
    factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="platform-saas-central-lava")

    @router.message(F.text.regexp(START_LAVA))
    async def open_lava_from_original_bot(message: Message, bot: Bot) -> None:
        if (
            message.chat.type != "private"
            or message.from_user is None
            or message.chat.id != message.from_user.id
            or not message.text
        ):
            return
        match = START_LAVA.fullmatch(message.text)
        if match is None:
            return
        try:
            await send_platform_lava_email_prompt(
                bot, factory,
                payer_telegram_id=message.from_user.id,
                invoice_id=decode_uuid(match.group(1)),
            )
        except Exception:
            LOG.warning("Central Lava.top email request unavailable")
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Lava.top пока недоступен для этого счёта. "
                    "Вернитесь к способам оплаты.",
                    "⚠️ Lava.top is not available for this invoice. "
                    "Return to payment methods.",
                ),
            )

    @router.message(_LavaEmailReplyFilter())
    async def accept_lava_email_reply(message: Message, bot: Bot) -> None:
        reply = message.reply_to_message
        if reply is None or message.from_user is None:
            return
        key = (message.from_user.id, reply.message_id)
        request = _pending_lava.pop(key, None)
        if request is None:
            return
        if (
            monotonic() >= request.expires_at
            or request.central_bot_id != bot.id
            or reply.from_user is None
            or reply.from_user.id != bot.id
        ):
            await bot.send_message(
                message.chat.id,
                tr(
                    "⌛ Запрос email истёк. Откройте оплату Lava.top заново.",
                    "⌛ Email request expired. Reopen Lava.top payment.",
                ),
            )
            return
        try:
            email = validate_lava_customer_email(message.text)
        except ValueError:
            try:
                await send_platform_lava_email_prompt(
                    bot, factory,
                    payer_telegram_id=message.from_user.id,
                    invoice_id=request.invoice_id,
                )
            except Exception:
                LOG.warning("Could not renew a Lava.top email prompt")
                await bot.send_message(
                    message.chat.id,
                    tr(
                        "⚠️ Не удалось запросить email повторно. "
                        "Откройте счёт заново.",
                        "⚠️ Could not renew the email prompt. "
                        "Reopen the invoice.",
                    ),
                )
            return
        try:
            link = await issue_platform_lava_checkout(
                factory, settings=get_settings(),
                payer_telegram_id=message.from_user.id,
                central_bot_id=bot.id, invoice_id=request.invoice_id,
                customer_email=email,
            )
        except Exception:
            # An API failure does not prove that a previous provider charge
            # did not happen. Never invent success or ask for a second payment.
            LOG.warning("Lava.top checkout link could not be issued")
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Ссылка Lava.top недоступна. Счёт не отмечен "
                    "оплаченным. Если вы уже платили, не платите повторно "
                    "и обратитесь в /paysupport.",
                    "⚠️ Lava.top checkout link unavailable. The invoice "
                    "is not marked paid. If you have already paid, do not "
                    "pay again; contact /paysupport.",
                ),
            )
            return
        await bot.send_message(
            message.chat.id,
            tr(
                "💳 <b>Счёт Lava.top создан</b>\n"
                "К оплате: <b>{amount:.2f} ₽</b>\n"
                "После успешного платежа ViMi Pay самостоятельно "
                "проверит его и продлит обслуживание.",
                "💳 <b>Lava.top checkout ready</b>\n"
                "Amount: <b>{amount:.2f} RUB</b>\n"
                "After payment, ViMi Pay will independently verify "
                "settlement and renew your bot's service.",
            ).format(amount=link.amount_rub),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr("💳 Открыть Lava.top", "💳 Open Lava.top"),
                    url=link.invoice_url,
                ),
            ]]),
        )

    return router
