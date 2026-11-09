"""Opt-in rich file checkout with a safe legacy Telegram message fallback.

Only visuals change. Existing inline keyboard callback_data and purchase
services remain unchanged. No retries on transport failures to avoid doubles.
"""
from __future__ import annotations

import inspect
import logging
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import InlineKeyboardMarkup

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import classic_messages

log = logging.getLogger(__name__)


def rich_file_checkout_enabled(bot_id: int) -> bool:
    """Disabled by default. Allow only explicit connected Telegram bot IDs."""
    settings = get_settings()
    if hasattr(settings, "rich_messages_enabled"):
        return settings.rich_messages_enabled
    ids = settings.rich_file_checkout_bot_ids
    return str(bot_id) in {part.strip() for part in ids.split(",") if part.strip()}


def rich_file_card_html(
    *, product_name: str, description: str | None, price: str,
    file_count: int, preview: bool = False,
) -> str:
    """Rich HTML for supported Telegram clients, never interpolating raw user HTML."""
    title = tr("💎 Покупка файла", "💎 File purchase")
    label_name = tr("🏷 Название", "🏷 Name")
    label_price = tr("💰 Цена", "💰 Price")
    label_files = tr("📎 Файлов", "📎 Files")
    prompt = (
        tr("✨ Предпросмотр оформления товара", "✨ Product design preview")
        if preview else
        tr("👇 Выберите способ оплаты ниже", "👇 Select a payment method below")
    )
    parts = [
        "<h3>" + title + "</h3>",
        "<table bordered compact>",
        "<tr><th>" + label_name + "</th><td>" + escape(product_name) + "</td></tr>",
        "<tr><th>" + label_price + "</th><td>" + escape(price) + "</td></tr>",
        "<tr><th>" + label_files + "</th><td>" + str(file_count) + "</td></tr>",
        "</table>",
    ]
    if description:
        paragraphs = "".join(
            "<p>" + escape(line) + "</p>"
            for line in description.splitlines() if line.strip()
        )
        if paragraphs:
            parts.append(
                "<details><summary>"
                + tr("📝 Описание", "📝 Description")
                + "</summary>" + paragraphs + "</details>"
            )
    parts.append("<p><b>" + prompt + "</b></p>")
    return "\n".join(parts)


async def send_file_card(
    bot: Bot, *, chat_id: int, rich_html: str, legacy_html: str,
    reply_markup: InlineKeyboardMarkup,
    preview: bool = False,
) -> bool:
    """Return True if Rich succeeded, False for the unchanged legacy screen.

    Telegram may not recognize the method or rich HTML on older Bot API
    clients. Fall back only on API validation/method errors, not on transport
    failures (where a retry could result in duplicate messages).
    """
    if getattr(get_settings(), "rich_messages_enabled", True) and (
        preview or rich_file_checkout_enabled(bot.id)
    ):
        try:
            from aiogram.types import InputRichMessage
        except ImportError:
            log.info("Rich Messages not supported by installed aiogram")
        else:
            send_rich = getattr(bot, "send_rich_message", None)
            if inspect.iscoroutinefunction(send_rich):
                try:
                    await send_rich(
                        chat_id=chat_id,
                        rich_message=InputRichMessage(html=rich_html),
                        reply_markup=reply_markup,
                    )
                    return True
                except TelegramBadRequest as exc:
                    log.warning(
                        "Rich file card API rejected formatting for bot %s: %s",
                        bot.id, exc,
                    )
                except TelegramNotFound as exc:
                    if "method not found" not in str(exc).lower():
                        raise
                    log.warning("sendRichMessage unsupported for bot %s", bot.id)

    with classic_messages():
        await bot.send_message(
            chat_id, legacy_html, parse_mode="HTML", reply_markup=reply_markup,
        )
    return False


def rich_admin_folders(folders) -> str:
    """Short management overview; all folder actions remain in the keyboard."""
    count = len(folders)
    active = sum(folder.status == "active" for folder in folders)
    lines = [
        "<h3>" + tr("📁 Управление папками", "📁 Manage folders") + "</h3>",
        "<p>" + tr("Всего: ", "Total: ") + str(count)
        + " · " + tr("Активных: ", "Active: ") + str(active) + "</p>",
    ]
    if folders:
        lines.extend([
            "<table bordered compact>",
            "<tr><th>" + tr("Папка", "Folder") + "</th><th>"
            + tr("Статус", "Status") + "</th></tr>",
        ])
        for folder in folders[:10]:
            status = (tr("🟢 Активна", "🟢 Active")
                      if folder.status == "active" else
                      tr("⚪ Архив", "⚪ Archived"))
            lines.append("<tr><td>" + escape(folder.name)
                         + "</td><td>" + status + "</td></tr>")
        lines.append("</table>")
        if count > 10:
            lines.append("<p>" + tr(
                "Показаны первые 10. Все папки доступны кнопками ниже.",
                "First 10 shown. Use the buttons to open any folder.",
            ) + "</p>")
    else:
        lines.append("<p>" + tr("Папок пока нет.", "No folders yet.") + "</p>")
    lines.append("<p>" + tr("👇 Выберите папку", "👇 Select a folder") + "</p>")
    return "\n".join(lines)


def rich_admin_products(products) -> str:
    """Owner folder inventory without leaking draft products to buyers."""
    total = len(products)
    published = sum(item.status == "published" for item in products)
    lines = [
        "<h3>" + tr("📦 Товары папки", "📦 Folder products") + "</h3>",
        "<p>" + tr("Всего: ", "Total: ") + str(total)
        + " · " + tr("В продаже: ", "Published: ") + str(published) + "</p>",
    ]
    if products:
        lines.extend([
            "<table bordered compact>",
            "<tr><th>" + tr("Название", "Name") + "</th><th>"
            + tr("Статус", "Status") + "</th></tr>",
        ])
        states = {
            "draft": tr("📝 Черновик", "📝 Draft"),
            "published": tr("🟢 В продаже", "🟢 Published"),
            "archived": tr("⚪ Архив", "⚪ Archived"),
        }
        for product in products[:10]:
            lines.append("<tr><td>" + escape(product.name[:100])
                         + "</td><td>" + escape(
                             states.get(product.status, product.status),
                         ) + "</td></tr>")
        lines.append("</table>")
        if total > 10:
            lines.append("<p>" + tr(
                "Показаны первые 10. Все товары доступны кнопками ниже.",
                "First 10 shown. All products remain available below.",
            ) + "</p>")
    else:
        lines.append("<p>" + tr("Товаров пока нет.", "No products yet.") + "</p>")
    lines.append("<p>" + tr("👇 Выберите товар", "👇 Select a product") + "</p>")
    return "\n".join(lines)


def rich_admin_product_detail(item, *, file_count: int, prices) -> str:
    """Manage one product, preserving raw amounts and all original actions."""
    status = {
        "draft": tr("📝 Черновик", "📝 Draft"),
        "published": tr("🟢 В продаже", "🟢 Published"),
        "archived": tr("⚪ Снят с продажи", "⚪ Archived"),
    }.get(item.status, item.status)
    lines = [
        "<h3>" + tr("📦 Управление товаром", "📦 Manage product") + "</h3>",
        "<table bordered compact>",
        "<tr><th>" + tr("🏷 Название", "🏷 Name") + "</th><td>"
        + escape(item.name) + "</td></tr>",
        "<tr><th>" + tr("📎 Файлов", "📎 Files") + "</th><td>"
        + str(file_count) + "</td></tr>",
        "<tr><th>" + tr("📌 Статус", "📌 Status") + "</th><td>"
        + escape(status) + "</td></tr>",
        "<tr><th>" + tr("⭐ Звёзды", "⭐ Stars") + "</th><td>"
        + (str(item.stars_price) + " XTR" if item.stars_price is not None
           else tr("Не установлена", "Not set")) + "</td></tr>",
        "</table>",
        "<p><b>" + tr("💱 Ручные цены", "💱 Manual prices") + "</b></p>",
    ]
    priced = [(currency, amount) for currency, amount in prices
              if amount is not None]
    if priced:
        lines.extend([
            "<table bordered compact>",
            "<tr><th>" + tr("Валюта", "Currency") + "</th><th>"
            + tr("Цена", "Price") + "</th></tr>",
        ])
        for currency, amount in priced:
            value = format(amount, "f")
            if "." in value:
                value = value.rstrip("0").rstrip(".")
            lines.append("<tr><td>" + escape(currency) + "</td><td>"
                         + escape(value) + "</td></tr>")
        lines.append("</table>")
    else:
        lines.append("<p>" + tr("Нет ручных цен", "No manual prices") + "</p>")
    if item.description:
        description = "".join(
            "<p>" + escape(line) + "</p>"
            for line in item.description.splitlines() if line.strip()
        )
        if description:
            lines.append(
                "<details><summary>" + tr("📝 Описание", "📝 Description")
                + "</summary>" + description + "</details>"
            )
    lines.append("<p>" + tr(
        "👇 Управляйте товаром кнопками ниже",
        "👇 Use the buttons below to manage this product",
    ) + "</p>")
    return "\n".join(lines)
