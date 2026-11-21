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

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import BaseFilter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.bots.service import TOKEN_PATTERN
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_gateway_service import (
    issue_platform_lava_checkout,
)
from telegram_membership_saas.modules.platform_saas_lava_client import (
    LAVA_LIMITS,
    LavaTopError,
    validate_lava_customer_email,
)
from telegram_membership_saas.modules.platform_saas_payment_redirect import (
    short_lava_url,
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


def clear_pending_lava_after_owner_navigation(
    *, payer_id: int, central_bot_id: int,
) -> None:
    """Disarm email entry only AFTER owner navigation passed its own guard."""
    for key, request in tuple(_pending_lava.items()):
        if key[0] == payer_id and request.central_bot_id == central_bot_id:
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
            "Отправьте адрес email <b>обычным сообщением</b> "
            "для выставления счёта. "
            "Адрес передаётся Lava.top, а не владельцу канала. "
            "Это ещё не платёж.",
            "💳 <b>Lava.top · ViMi Pay payment</b>\n"
            "Amount: <b>{amount:.2f} RUB</b>\n\n"
            "Send your email address <b>as a normal message</b> "
            "to create the checkout. "
            "The address is sent to Lava.top, not the channel owner. "
            "No payment is made yet.",
        ).format(amount=remaining),
        parse_mode="HTML",
        # One in-message cancel action, bound to this prompt's message_id.
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=tr("✖️ Отмена", "✖️ Cancel"),
                callback_data="lava:cancel", style="primary",
            ),
        ]]),
    )
    _pending_lava[(payer_telegram_id, prompt.message_id)] = _PendingEmail(
        invoice_id=invoice_id, central_bot_id=bot.id,
        expires_at=monotonic() + EMAIL_REPLY_SECONDS,
    )


def _current_prompt(
    payer_id: int, bot_id: int,
) -> tuple[tuple[int, int], _PendingEmail] | None:
    # There is at most one active email prompt for a payer; retain the
    # prompt message ID so replies to unrelated Telegram messages are not
    # accidentally interpreted as checkout information.
    for key, request in tuple(_pending_lava.items()):
        if key[0] == payer_id and request.central_bot_id == bot_id:
            return key, request
    return None


class _LavaEmailReplyFilter(BaseFilter):
    async def __call__(self, message: Message, bot: Bot) -> bool:
        if (
            message.chat.type != "private"
            or message.from_user is None
            or message.from_user.id != message.chat.id
            or not isinstance(message.text, str)
            or not message.text
            or message.text.startswith("/")
            or TOKEN_PATTERN.fullmatch(message.text.strip()) is not None
        ):
            # Never swallow BotFather tokens before the existing security
            # handler can delete and warn about credential exposure.
            return False
        current = _current_prompt(message.from_user.id, bot.id)
        if current is None:
            return False
        reply = message.reply_to_message
        if reply is None:
            return True  # Normal typed email, with no stuck reply composer.
        key, _ = current
        return bool(
            reply.message_id == key[1]
            and reply.from_user is not None
            and reply.from_user.id == bot.id
        )


class ClearPendingLavaOnNavigation(BaseMiddleware):
    """Disarm stale email capture before unrelated central navigation.

    Other owner/account flows then receive their next plain input normally.
    Only *actual* navigation actions for the same private Telegram user
    invalidate their own pending email. No customer bots use this middleware.
    """

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        chat = (
            getattr(event, "chat", None) if isinstance(event, Message)
            else getattr(getattr(event, "message", None), "chat", None)
        )
        if (
            user is not None and chat is not None
            and chat.type == "private" and chat.id == user.id
        ):
            navigating = (
                (
                    isinstance(event, CallbackQuery)
                    and event.data != "lava:cancel"
                    # The owning router must validate the prompt generation
                    # before this input can be cleared by navigation.
                    and not (event.data or "").startswith((
                        "own:promptback:", "own:promptcancel:",
                    ))
                )
                or (
                    isinstance(event, Message)
                    and isinstance(event.text, str)
                    and event.text.startswith("/")
                    and event.text not in (
                        "/cancel", "/cancel@vimipay_bot",
                    )
                )
            )
            if navigating:
                for key in tuple(_pending_lava):
                    if key[0] == user.id:
                        _pending_lava.pop(key, None)
        return await handler(event, data)


class _LavaCancelFilter(BaseFilter):
    async def __call__(self, message: Message, bot: Bot) -> bool:
        return bool(
            message.chat.type == "private"
            and message.from_user is not None
            and message.from_user.id == message.chat.id
            and message.text in ("/cancel", "/cancel@vimipay_bot")
            and _current_prompt(message.from_user.id, bot.id) is not None
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

    @router.callback_query(F.data == "lava:cancel")
    async def cancel_lava_email_button(query: CallbackQuery, bot: Bot) -> None:
        """Cancel only this prompt; never disarm a newer invoice's email input."""
        if (
            query.message is None
            or query.message.chat.type != "private"
            or query.from_user is None
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer(tr("Нет доступа", "Access denied"), show_alert=True)
            return
        current = _current_prompt(query.from_user.id, bot.id)
        message_id = getattr(query.message, "message_id", None)
        if (
            current is None or current[0][1] != message_id
            or monotonic() >= current[1].expires_at
        ):
            await query.answer(
                tr("Форма устарела", "This form has expired"), show_alert=True,
            )
            return
        _pending_lava.pop(current[0], None)
        await query.answer()
        await bot.send_message(
            query.from_user.id,
            tr(
                "✅ Ввод email для Lava.top отменён. Счёт не изменён.",
                "✅ Lava.top email entry cancelled. Invoice unchanged.",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr("🧾 Счета ViMi Pay", "🧾 ViMi Pay invoices"),
                    callback_data="own:saasinvoices:0", style="primary",
                ),
            ]]),
        )

    @router.message(_LavaCancelFilter())
    async def cancel_lava_email(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        current = _current_prompt(message.from_user.id, bot.id)
        if current is None:
            return
        _pending_lava.pop(current[0], None)
        await bot.send_message(
            message.chat.id,
            tr(
                "✅ Ввод email для Lava.top отменён. Счёт не изменён. "
                "Откройте 🧾 Счета ViMi Pay.",
                "✅ Lava.top email entry cancelled. Invoice unchanged. "
                "Open 🧾 ViMi Pay invoices.",
            ),
            reply_markup=ReplyKeyboardRemove(),
        )

    @router.message(_LavaEmailReplyFilter())
    async def accept_lava_email_reply(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        current = _current_prompt(message.from_user.id, bot.id)
        if current is None:
            return
        key, request = current
        _pending_lava.pop(key, None)
        if monotonic() >= request.expires_at:
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
        except Exception as exc:
            # No provider response, email, request headers or exception stack
            # is emitted. The code is a hard-bounded diagnostic from our own
            # adapter (e.g. LAVA-HTTP-400-CURRENCY, LAVA-HTTP-401).
            code = exc.code if isinstance(exc, LavaTopError) else "LAVA-CHECKOUT"
            if not re.fullmatch(r"LAVA-[A-Z0-9-]{3,55}", code):
                code = "LAVA-CHECKOUT"
            LOG.warning("ViMi Pay platform Lava.top checkout failed: code=%s", code)
            advice = (
                tr(
                    " Проверьте включение RUB в платёжных настройках Lava.top.",
                    " Check RUB payment acceptance settings in Lava.top.",
                ) if code == "LAVA-HTTP-400-CURRENCY" else
                tr(
                    " Проверьте API-ключ ViMi Pay в Lava.top.",
                    " Check the ViMi Pay Lava.top API key.",
                ) if code in ("LAVA-HTTP-401", "LAVA-HTTP-403") else
                tr(
                    " Проверьте offerId опубликованного продукта.",
                    " Check the published product offerId.",
                ) if code in ("LAVA-HTTP-400-OFFER", "LAVA-HTTP-404") else ""
            )
            await bot.send_message(
                message.chat.id,
                tr(
                    "⚠️ Не удалось создать ссылку Lava.top. "
                    "Счёт не отмечен оплаченным. Если вы уже платили, "
                    "не платите повторно. Обратитесь в /paysupport.\n"
                    "Код диагностики: <code>{code}</code>.",
                    "⚠️ Lava.top checkout link unavailable. "
                    "Invoice was not marked paid. If you already paid, "
                    "do not pay again. Contact /paysupport.\n"
                    "Diagnostic code: <code>{code}</code>.",
                ).format(code=code) + advice,
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        # Shortlinks are a central-only opt-in after the isolated public API
        # and dedicated HTTPS /p/ ingress have passed acceptance. Without
        # approval, retain the original working provider checkout URL.
        url = link.invoice_url
        if get_settings().saas_shortlinks_enabled:
            try:
                url = short_lava_url(link.order_id)
            except ValueError:
                LOG.warning("Invalid platform Lava order identity for shortlink")
        await bot.send_message(
            message.chat.id,
            tr(
                "💳 <b>Счёт Lava.top создан</b>\n"
                "К оплате: <b>{amount:.2f} {currency}</b>\n"
                "После успешного платежа ViMi Pay самостоятельно "
                "проверит его и продлит обслуживание.",
                "💳 <b>Lava.top checkout ready</b>\n"
                "Amount: <b>{amount:.2f} {currency}</b>\n"
                "After payment, ViMi Pay will independently verify "
                "settlement and renew your bot's service.",
            ).format(
                amount=link.pay_amount if link.pay_amount is not None else link.amount_rub,
                currency=link.pay_currency,
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr(
                        "💳 Оплатить {amount:.2f} {currency}",
                        "💳 Pay {amount:.2f} {currency}",
                    ).format(
                        amount=link.pay_amount if link.pay_amount is not None
                        else link.amount_rub,
                        currency=link.pay_currency,
                    ),
                    url=url,
                ),
            ]]),
        )

    return router
