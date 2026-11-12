"""Shared RU/EN Rich Crypto Pay receipt for subscriptions and file purchases.

Use the existing rich-checkout sender and existing purchase-history navigation.
The provider URL comes ONLY from the verified createInvoice/recovery service.
"""
from __future__ import annotations

from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas.currency_display import display_price
from telegram_membership_saas.localization import tr
from telegram_membership_saas.manual_checkout_ui import send_manual_checkout
from telegram_membership_saas.modules.crypto_pay.buyer import BuyerCryptoInvoice
from telegram_membership_saas.modules.telegram_commerce.service import format_period


def _amount(value) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


async def send_buyer_crypto_invoice_screen(
    bot: Bot, *, buyer_id: int, invoice: BuyerCryptoInvoice,
    back_callback: str,
) -> None:
    """One payable, verified Telegram deep link; zero fake callback buttons."""
    if invoice.kind not in ("file", "subscription"):
        raise ValueError("Unsupported Crypto Pay buyer checkout")
    subscription = invoice.kind == "subscription"
    title = (
        tr("💎 Оформление подписки", "💎 Subscription checkout")
        if subscription
        else tr("💎 Оформление покупки файла", "💎 File purchase checkout")
    )
    label = (
        tr("🎟 Тариф", "🎟 Plan") if subscription
        else tr("🏷 Название", "🏷 Name")
    )
    price = display_price(_amount(invoice.amount), invoice.currency)
    period = (
        format_period(invoice.duration_value, invoice.duration_unit)
        if subscription and invoice.duration_unit else None
    )
    instruction = tr(
        "Перейдите в Crypto Pay по кнопке ниже. После подтверждения "
        "платежа бот автоматически выдаст доступ или отправит файлы. "
        "Чек присылать не нужно.",
        "Open Crypto Pay using the button below. Once the payment is "
        "confirmed, the bot grants access or sends your files automatically. "
        "No receipt upload is required.",
    )
    history = tr(
        "Подписка будет в разделе «Мои подписки».",
        "Your membership will appear in My subscriptions.",
    ) if subscription else tr(
        "Файлы всегда можно получить повторно через /myfiles.",
        "You can redownload your files anytime with /myfiles.",
    )
    rich = [
        "<h3>" + title + "</h3>",
        "<table bordered compact>",
        "<tr><th>" + label + "</th><td>" + escape(invoice.name) + "</td></tr>",
    ]
    if period:
        rich.append(
            "<tr><th>" + tr("🗓️ Срок", "🗓️ Duration")
            + "</th><td>" + escape(period) + "</td></tr>"
        )
    rich.extend([
        "<tr><th>" + tr("💰 Цена", "💰 Price")
        + "</th><td>" + escape(price) + "</td></tr>",
        "<tr><th>" + tr("💳 Способ", "💳 Method")
        + "</th><td>💎 Crypto Pay</td></tr>",
        "</table>",
        "<p>" + escape(instruction) + "</p>",
        "<p>" + escape(history) + "</p>",
    ])
    old = (
        title.replace("<h3>", "<b>").replace("</h3>", "</b>")
        + "\n\n" + label + ": <b>" + escape(invoice.name) + "</b>\n"
        + (tr("🗓️ Срок: ", "🗓️ Duration: ") + escape(period) + "\n"
           if period else "")
        + tr("💰 Цена: ", "💰 Price: ") + "<b>" + escape(price) + "</b>\n"
        + "💎 Crypto Pay\n\n"
        + escape(instruction) + "\n\n" + escape(history)
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=tr("💎 Перейти к оплате", "💎 Open Crypto Pay"),
            url=invoice.url, style="success",
        )],
        [InlineKeyboardButton(
            text=tr("⬅️ Назад", "⬅️ Back"),
            callback_data=back_callback,
        )],
    ])
    # Empty 'instruction' disables only the optional bank-detail copy button,
    # while preserving the shared Rich/legacy fallback and the exact deep link.
    await send_manual_checkout(
        bot, chat_id=buyer_id,
        rich_html="\n".join(rich), legacy_html=old,
        instruction="", reply_markup=keyboard,
    )
