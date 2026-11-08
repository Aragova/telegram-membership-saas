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
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import (
    InputRichMessage,
    classic_messages,
    rich_enabled,
)

log = logging.getLogger(__name__)

# No speculative parsing of arbitrary bank details: a bare card/account
# number or a clearly labelled phone/card/account field only.
_ACCOUNT_NUMBER = re.compile(r"(?:\\d[ -]?){15,19}\\d")
_LABELED_VALUE = re.compile(
    r"^(?:карта|номер карты|сч[её]т|номер сч[её]та|сбп|телефон|"
    r"card|card number|account|account number|phone|iban)\\s*[:：]\\s*"
    r"(?P<value>\\+?\\d[\\d() -]{6,31})$",
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
            if 8 <= len(re.sub(r"\\D", "", value)) <= 20:
                candidates.add(value)
    if len(candidates) != 1:
        return None
    result = next(iter(candidates))
    return result if 1 <= len(result) <= 256 else None


def copyable_checkout_keyboard(
    original: InlineKeyboardMarkup, instruction: str,
) -> InlineKeyboardMarkup:
    """Add a native Telegram clipboard button without changing any callbacks."""
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
