"""Shared visual-only manual payment checkout for files and subscriptions.

Buyer payment instructions stay unchanged. Copy buttons are shown only when a
single payment detail can be recognized unambiguously; they never mutate orders.
"""
from __future__ import annotations

import inspect
import logging
import re
from html import escape
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import AiogramError, TelegramBadRequest, TelegramNotFound
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

try:
    from aiogram.types import CopyTextButton
except ImportError:  # Older supported aiogram installations (3.13)
    CopyTextButton = None

try:
    from aiogram.types import DisabledButton
except ImportError:  # Older supported aiogram installations
    DisabledButton = None

from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import (
    InputRichMessage,
    classic_messages,
    rich_enabled,
)

log = logging.getLogger(__name__)

# No speculative parsing of arbitrary bank details: a bare card/account
# number or a clearly labelled phone/card/account field only.
_ACCOUNT_NUMBER = re.compile(r"(?:\d[ -]?){15,19}\d")
_LABELED_VALUE = re.compile(
    r"^(?:карта|номер карты|сч[её]т|номер сч[её]та|сбп|телефон|"
    r"card|card number|account|account number|phone|iban)\s*[:：]\s*"
    r"(?P<value>\+?\d[\d() -]{6,31})$",
    flags=re.IGNORECASE,
)


def copyable_payment_detail(instruction: str) -> str | None:
    """Return exact owner-authored detail, or None when ambiguous/unsupported."""
    candidates: set[str] = set()
    for raw_line in instruction.splitlines():
        line = raw_line.strip()
        if _ACCOUNT_NUMBER.fullmatch(line):
            candidates.add(line)
            continue
        labeled = _LABELED_VALUE.fullmatch(line)
        if labeled:
            value = labeled.group("value").strip()
            if 8 <= len(re.sub(r"\D", "", value)) <= 20:
                candidates.add(value)
    if len(candidates) != 1:
        return None
    result = next(iter(candidates))
    return result if 1 <= len(result) <= 256 else None


def copyable_checkout_keyboard(
    original: InlineKeyboardMarkup, instruction: str,
) -> InlineKeyboardMarkup:
    """Add a native Telegram clipboard button without changing any callbacks."""
    if CopyTextButton is None:
        return original
    detail = copyable_payment_detail(instruction)
    if detail is None:
        return original
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=tr("📋 Скопировать реквизиты", "📋 Copy payment details"),
            copy_text=CopyTextButton(text=detail),
        )],
        *original.inline_keyboard,
    ])


def manual_checkout_rich_html(
    *, kind: Literal["file", "subscription"],
    name: str | None, period: str | None, amount: str, currency: str,
    payment_method: str, instruction: str, guidance_html: str,
) -> str:
    """Make a bilingual Rich card; only the fixed guidance is trusted HTML."""
    if kind not in ("file", "subscription"):
        raise ValueError("Unsupported checkout kind")
    title = (tr("💎 Оформление покупки файла", "💎 File purchase checkout")
             if kind == "file" else
             tr("💎 Оформление подписки", "💎 Subscription checkout"))
    lines = ["<h3>" + title + "</h3>", "<table bordered compact>"]
    if name:
        label = (tr("🏷 Название", "🏷 Name") if kind == "file" else
                 tr("🎟 Тариф", "🎟 Plan"))
        lines.append(
            "<tr><th>" + label + "</th><td>" + escape(name) + "</td></tr>"
        )
    if period is not None:
        lines.append(
            "<tr><th>" + tr("⏳ Срок", "⏳ Duration") + "</th><td>"
            + escape(period) + "</td></tr>"
        )
    lines.extend([
        "<tr><th>" + tr("💰 Цена", "💰 Price") + "</th><td>"
        + escape(amount + " " + currency) + "</td></tr>",
        "<tr><th>" + tr("💳 Способ", "💳 Method") + "</th><td>"
        + escape(payment_method) + "</td></tr>",
        "</table>",
        "<p><b>" + tr("🏦 Реквизиты для оплаты", "🏦 Payment details")
        + "</b></p>",
        # Telegram Rich HTML collapses literal newlines; <br> restores them
        # while preserving the original instructions as visible plain text.
        "<p>" + escape(instruction).replace("\r\n", "\n").replace(
            "\r", "\n",
        ).replace("\n", "<br>") + "</p>",
        "<p>" + guidance_html + "</p>",
    ])
    return "\n".join(lines)


async def send_manual_checkout(
    bot: Bot, *, chat_id: int, rich_html: str, legacy_html: str,
    instruction: str, reply_markup: InlineKeyboardMarkup,
) -> None:
    """Send Rich once; fallback to original HTML only on definite API rejection.

    Network errors are propagated, not retried, because a payment instruction
    could already have reached the buyer. Legacy keyboard remains untouched.
    """
    if rich_enabled() and InputRichMessage is not None:
        send_rich = getattr(bot, "send_rich_message", None)
        if inspect.iscoroutinefunction(send_rich) and len(
            rich_html.encode("utf-8")
        ) < 32768:
            try:
                await send_rich(
                    chat_id=chat_id,
                    rich_message=InputRichMessage(html=rich_html),
                    reply_markup=copyable_checkout_keyboard(
                        reply_markup, instruction,
                    ),
                )
                return
            except TelegramBadRequest as exc:
                reason = str(exc).lower()
                if not any(token in reason for token in (
                    "rich", "can't parse", "copy_text", "unsupported",
                )):
                    raise
                log.info("Rich manual checkout rejected; using classic HTML")
            except TelegramNotFound as exc:
                if "method not found" not in str(exc).lower():
                    raise
                log.info("Rich checkout method unavailable; using classic HTML")
    with classic_messages():
        await bot.send_message(
            chat_id, legacy_html, parse_mode="HTML", reply_markup=reply_markup,
        )


def cancelled_checkout_keyboard(
    *, navigation: InlineKeyboardButton | None = None,
) -> InlineKeyboardMarkup | None:
    """Replace payment, copy, and cancellation actions with a final UI state.

    DisabledButton is optional for older supported aiogram versions. Never use
    a fake callback as a status indicator: it could trigger unintended actions.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if DisabledButton is not None:
        rows.append([InlineKeyboardButton(
            text=tr("❌ Заявка отменена", "❌ Request canceled"),
            disabled=DisabledButton(),
            style="danger",
        )])
    if navigation is not None:
        rows.append([navigation])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def retire_cancelled_checkout(
    bot: Bot, *, callback, navigation: InlineKeyboardButton | None = None,
) -> bool:
    """Best-effort UI retirement only AFTER the database confirms cancellation.

    Do not rewrite a Rich card, because its media, payment details and customer-
    authored instructions could be lost. Editing the keyboard is idempotent.
    A Telegram failure must not roll back or disguise the committed cancellation.
    """
    message = getattr(callback, "message", None)
    chat = getattr(message, "chat", None)
    buyer = getattr(callback, "from_user", None)
    message_id = getattr(message, "message_id", None)
    if (
        chat is None or getattr(chat, "type", None) != "private"
        or buyer is None or getattr(chat, "id", None) != buyer.id
        or not isinstance(message_id, int)
    ):
        return False
    kwargs = {"chat_id":chat.id, "message_id":message_id}
    markup = cancelled_checkout_keyboard(navigation=navigation)
    try:
        await bot.edit_message_reply_markup(**kwargs, reply_markup=markup)
        return True
    except TelegramBadRequest as exc:
        reason = str(exc).lower()
        if "message is not modified" in reason:
            return True
        # Some Telegram deployments reject newer disabled buttons. Remove
        # the stale payment buttons anyway while retaining safe navigation.
        if DisabledButton is not None and any(key in reason for key in (
            "disabled", "can't parse", "unsupported", "inline keyboard",
        )):
            fallback = (
                InlineKeyboardMarkup(inline_keyboard=[[navigation]])
                if navigation is not None else None
            )
            try:
                await bot.edit_message_reply_markup(
                    **kwargs, reply_markup=fallback,
                )
                return True
            except AiogramError:
                log.warning("Could not retire canceled checkout keyboard", exc_info=True)
                return False
        log.warning("Could not retire canceled checkout keyboard: %s", exc)
        return False
    except AiogramError:
        log.warning("Could not retire canceled checkout keyboard", exc_info=True)
        return False
