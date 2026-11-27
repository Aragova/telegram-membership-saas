"""Shared Stars checkout presentation; never creates or settles an order."""
import logging
import re
from html import escape
from pathlib import PurePath
from traceback import walk_tb

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas.localization import tr


def log_stars_checkout_failure(
    logger: logging.Logger, operation: str, exc: Exception,
) -> None:
    """Log stack locations and SQLSTATE, never exception text or frame locals."""
    orig = getattr(exc, "orig", None)
    state = getattr(orig, "sqlstate", None)
    if not isinstance(state, str) or re.fullmatch(r"[0-9A-Z]{5}", state) is None:
        state = "-"
    frames = " > ".join(
        f"{PurePath(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}"
        for frame, line in walk_tb(exc.__traceback__)
    )
    logger.warning(
        "%s: %s; SQLSTATE=%s; stack=%s",
        operation, type(exc).__name__, state, frames or "-",
    )


def stars_topup_guidance() -> str:
    return tr(
        "💡 <b>Не хватает звёзд?</b>\n\n"
        "Telegram может показать ошибку при оплате. "
        "Заранее пополните баланс через официальный @PremiumBot "
        "или настройки Telegram.\n\n"
        "<b>Как оплатить:</b>\n"
        "① Купите необходимое количество звёзд.\n"
        "② Вернитесь в этот чат.\n"
        "③ Нажмите кнопку оплаты.",
        "💡 <b>Not enough Stars?</b>\n\n"
        "Telegram may show a payment error. Top up first using "
        "the official @PremiumBot or Telegram Settings.\n\n"
        "<b>How to pay:</b>\n"
        "① Buy the required number of Stars.\n"
        "② Return to this chat.\n"
        "③ Tap the payment button.",
    )


def stars_topup_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=tr("💫 Купить звёзды", "💫 Buy Stars"),
        url="https://t.me/PremiumBot", style="primary",
    )


def stars_checkout_rows(
    *, amount: int, link: str, back: InlineKeyboardButton,
) -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(
            text=tr("⭐ Оплатить {amount}", "⭐ Pay {amount}").format(amount=amount),
            url=link, style="success",
        )],
        [stars_topup_button()],
        [back],
    ]


def buyer_stars_checkout_html(
    *, title: str, details: list[tuple[str, str]], amount: int,
    notice: str = "",
) -> str:
    rows = "\n".join(
        escape(label) + ": <b>" + escape(value) + "</b>"
        for label, value in details
    )
    return (
        "<b>" + escape(title) + "</b>\n\n" + rows + "\n"
        + tr("💰 К оплате", "💰 Total due") + ": <b>" + str(amount) + " ⭐</b>\n\n"
        + stars_topup_guidance()
        + ("\n\n" + escape(notice) if notice else "")
    )


async def send_stars_screen(
    bot: Bot, *, chat_id: int, html: str, keyboard: InlineKeyboardMarkup,
    message_id: int | None,
) -> None:
    """Edit navigation; retry only an explicitly uneditable message."""
    if message_id is not None:
        try:
            await bot.edit_message_text(
                text=html, chat_id=chat_id, message_id=message_id,
                parse_mode="HTML", reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as exc:
            error = str(exc).lower()
            if "message is not modified" in error:
                return
            if not any(fragment in error for fragment in (
                "message to edit not found", "message can't be edited",
                "message can not be edited",
            )):
                raise
    await bot.send_message(
        chat_id=chat_id, text=html, parse_mode="HTML", reply_markup=keyboard,
    )
