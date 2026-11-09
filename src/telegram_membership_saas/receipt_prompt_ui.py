"""Clean bilingual receipt upload prompt without Telegram ForceReply composer."""
from __future__ import annotations

from html import escape
from uuid import UUID

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas.localization import tr
from telegram_membership_saas.payment_review_ui import send_payment_review


def receipt_prompt(
    *, kind: str, request_id: UUID, name: str | None = None,
    amount: str | None = None, currency: str | None = None,
) -> tuple[str, str]:
    """Display only a short public reference; full ID stays in callback/context."""
    title = (
        tr("💎 Подписка", "💎 Subscription") if kind == "subscription"
        else tr("📦 Покупка файла", "📦 File purchase")
    )
    short_ref = request_id.hex[:10].upper()
    rows = [
        "<tr><th>" + tr("Покупка", "Purchase") + "</th><td>"
        + escape(name or title) + "</td></tr>",
        "<tr><th>" + tr("Заявка", "Request") + "</th><td>"
        + short_ref + "</td></tr>",
    ]
    if amount is not None and currency:
        rows.append("<tr><th>" + tr("Сумма", "Amount") + "</th><td>"
                    + escape(amount) + " " + escape(currency) + "</td></tr>")
    guidance = tr(
        "Отправьте фотографию чека или файл PDF/JPG/PNG/WEBP "
        "обычным сообщением в этот чат. Нажимать «Ответить» не нужно.",
        "Send the receipt photo or PDF/JPG/PNG/WEBP file as a normal "
        "message to this chat. No need to use Reply.",
    )
    warning = tr(
        "Без чека заявка не поступит администратору. "
        "Выбор действует 30 минут; при необходимости снова нажмите «Я оплатил».",
        "The administrator won't receive this request without proof. "
        "This selection lasts 30 minutes; tap “I have paid” again if needed.",
    )
    rich = "\n".join([
        "<h3>" + tr("📎 Прикрепите чек", "📎 Attach your receipt") + "</h3>",
        "<table bordered compact>", *rows, "</table>",
        "<p>" + guidance + "</p>",
        "<blockquote>" + warning + "</blockquote>",
    ])
    classic = (
        "<b>" + tr("📎 Прикрепите чек", "📎 Attach your receipt") + "</b>\n"
        + title + " · № " + short_ref + "\n"
        + (tr("Сумма: ", "Amount: ") + escape(amount) + " "
           + escape(currency) + "\n" if amount is not None and currency else "")
        + "\n" + guidance + "\n\n" + warning
    )
    return rich, classic


async def send_receipt_prompt(
    bot: Bot, *, buyer_id: int, kind: str, request_id: UUID,
    cancel_data: str, name: str | None = None,
    amount: str | None = None, currency: str | None = None,
) -> None:
    rich, classic = receipt_prompt(
        kind=kind, request_id=request_id, name=name, amount=amount,
        currency=currency,
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=tr("✖️ Отменить отправку чека", "✖️ Stop uploading receipt"),
            callback_data=cancel_data, style="danger",
        ),
    ]])
    await send_payment_review(
        bot, chat_id=buyer_id, rich_html=rich,
        legacy_text=classic, parse_mode="HTML", reply_markup=keyboard,
    )
