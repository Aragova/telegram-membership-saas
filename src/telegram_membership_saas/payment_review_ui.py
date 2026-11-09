"""Unified read-only Rich UI for administrator payment reviews.

Payment decisions, receipt evidence, permissions and callbacks stay in their
existing handlers/services. Unsupported Rich endpoints get classic messages.
"""
from __future__ import annotations

import inspect
import logging
from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import InlineKeyboardMarkup, Message, ReplyParameters

from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import (
    InputRichMessage,
    classic_messages,
    rich_enabled,
)

if TYPE_CHECKING:
    from telegram_membership_saas.modules.telegram_commerce.service import (
        ReviewItem,
        ReviewResult,
    )

log = logging.getLogger(__name__)


def _escape(value: object) -> str:
    return escape(str(value), quote=True)


def _money(value: Decimal) -> str:
    amount = format(value, "f")
    return amount.rstrip("0").rstrip(".") if "." in amount else amount


def _row(label: str, value: object) -> str:
    return "<tr><th>" + _escape(label) + "</th><td>" + _escape(value) + "</td></tr>"


def _name(username: str | None, buyer_id: int) -> str:
    return "@" + username if username else str(buyer_id)


def rich_subscription_review(
    item: ReviewItem, *, receipt_attached: bool,
) -> str:
    """A subscription review card, with honest receipt context."""
    body = [
        "<h3>" + tr("🧾 Заявка на проверку", "🧾 Payment review") + "</h3>",
        "<table bordered compact>",
        _row(tr("👤 Покупатель", "👤 Customer"),
             _name(item.customer_username, item.customer_telegram_user_id)),
        _row("Telegram ID", item.customer_telegram_user_id),
        _row(tr("🎟 Тариф", "🎟 Plan"), item.plan_name),
        _row(tr("💰 Сумма", "💰 Amount"),
             _money(item.amount) + " " + item.currency_code),
        _row(tr("💳 Способ", "💳 Method"), item.payment_method_name),
        _row(tr("🔖 Заявка", "🔖 Request"), item.request_id),
        "</table>",
        "<p><b>" + (
            tr("📎 Чек: прикреплён выше", "📎 Receipt: attached above")
            if receipt_attached else tr(
                "📎 Проверьте оригинал чека в истории сообщений",
                "📎 Check the original receipt in message history",
            )
        ) + "</b></p>",
        "<p>" + tr(
            "Перед подтверждением проверьте поступление денег в банке.",
            "Verify funds in your bank before approving.",
        ) + "</p>",
    ]
    return "\n".join(body)


def rich_subscription_review_result(result: ReviewResult, *, approve: bool) -> str:
    """Admin decision confirmation, reflecting the committed service result."""
    item = result.item
    headline = (
        tr("✅ Оплата подтверждена", "✅ Payment approved") if approve
        else tr("❌ Оплата отклонена", "❌ Payment rejected")
    )
    body = [
        "<h3>" + headline + "</h3>",
        "<table bordered compact>",
        _row(tr("👤 Покупатель", "👤 Customer"),
             _name(item.customer_username, item.customer_telegram_user_id)),
        _row(tr("🎟 Тариф", "🎟 Plan"), item.plan_name),
        _row(tr("💰 Сумма", "💰 Amount"),
             _money(item.amount) + " " + item.currency_code),
        _row(tr("🔖 Заявка", "🔖 Request"), item.request_id),
        _row(tr("Статус", "Status"), tr("Подтверждена", "Approved")
             if approve else tr("Отклонена", "Rejected")),
    ]
    if approve:
        end = result.subscription_period_end
        until = (
            end.astimezone(ZoneInfo(result.project_timezone)).strftime(
                "%d.%m.%Y %H:%M",
            ) + " (" + result.project_timezone + ")"
            if end is not None else tr("Бессрочно", "Lifetime")
        )
        body.append(_row(tr("📅 Доступ до", "📅 Access until"), until))
    body.append("</table>")
    body.append("<p>" + (
        tr("Бот автоматически предоставит доступ покупателю.",
           "The bot will automatically grant the customer access.")
        if approve else tr("Заявка закрыта. Подписка не активирована.",
                           "Request closed. No subscription was activated.")
    ) + "</p>")
    return "\n".join(body)


def rich_file_review(order, *, receipt_attached: bool = False) -> str:
    """Rich summary of the exact authorized manual file-order snapshot."""
    order_status = getattr(
        order, "status", "submitted" if receipt_attached else "pending",
    )
    status = {
        "pending": tr("🕓 Ожидает оплату", "🕓 Awaiting payment"),
        "submitted": tr("🕒 На проверке", "🕒 Under review"),
        "approved": tr("✅ Подтверждена", "✅ Approved"),
        "rejected": tr("❌ Отклонена", "❌ Rejected"),
        "cancelled": tr("⚪ Отменена", "⚪ Cancelled"),
    }.get(order_status, order_status)
    body = [
        "<h3>" + tr("📦 Проверка покупки файла", "📦 File purchase review") + "</h3>",
        "<table bordered compact>",
        _row(tr("🏷 Название", "🏷 Name"), order.product_name),
        _row(tr("👤 Покупатель", "👤 Customer"),
             getattr(order, "telegram_user_id", "—")),
        _row(tr("💰 Сумма", "💰 Amount"),
             _money(order.amount) + " " + order.currency_code),
        _row(tr("💳 Способ", "💳 Method"),
             getattr(order, "payment_method_name", "—")),
        _row(tr("🧾 Статус", "🧾 Status"), status),
        _row(tr("🔖 Заявка", "🔖 Request"), order.id),
        "</table>",
    ]
    if receipt_attached:
        body.append("<p><b>" + tr(
            "📎 Чек отправлен сообщением выше.",
            "📎 Receipt sent in the message above.",
        ) + "</b></p>")
    elif order_status == "submitted":
        body.append("<p><b>" + tr(
            "📎 Чек отправлен отдельно. Проверьте его.",
            "📎 Receipt was sent separately. Please review it.",
        ) + "</b></p>")
    elif order_status == "pending":
        body.append("<p>" + tr(
            "Чек ещё не предоставлен. Не подтверждайте неоплаченный счёт.",
            "Receipt not submitted. Do not approve an unpaid invoice.",
        ) + "</p>")
    body.append("<p>" + tr(
        "Перед решением проверьте поступление средств в банке.",
        "Verify the bank transfer before deciding.",
    ) + "</p>")
    return "\n".join(body)


def rich_file_reviews_queue(items: tuple, *, page: int, total: int) -> str:
    """Compact file review list with the original per-order callback keyboard."""
    pages = max(1, (total + 7) // 8)
    body = [
        "<h3>" + tr("🧾 Заявки на оплату файлов", "🧾 File payment reviews")
        + "</h3>",
        "<p>" + tr("Всего: ", "Total: ") + str(total) + " · "
        + tr("Страница ", "Page ") + str(page + 1) + " / " + str(pages)
        + "</p>",
    ]
    if not items:
        body.append("<p>" + tr("Нет новых заявок.", "No pending reviews.") + "</p>")
        return "\n".join(body)
    body.extend([
        "<table bordered compact>",
        "<tr><th>" + tr("Товар", "Product") + "</th><th>"
        + tr("Сумма / статус", "Amount / status") + "</th></tr>",
    ])
    for item in items:
        status = (
            tr("📨 Чек получен", "📨 Receipt received") if item.status == "submitted"
            else tr("🕓 Ожидает оплату", "🕓 Awaiting payment")
        )
        body.append("<tr><td>" + _escape(item.product_name[:45])
                    + "</td><td>" + _escape(
                        _money(item.amount) + " " + item.currency_code,
                    ) + "<br>" + status + "</td></tr>")
    body.extend([
        "</table>",
        "<p>" + tr("👇 Откройте заявку ниже", "👇 Open a request below") + "</p>",
    ])
    return "\n".join(body)


def rich_file_close_warning(order) -> str:
    return "\n".join([
        "<h3>" + tr(
            "⚠️ Закрытие неоплаченного счёта",
            "⚠️ Close unpaid invoice",
        ) + "</h3>",
        "<table bordered compact>",
        _row(tr("🏷 Название", "🏷 Name"), order.product_name),
        _row(tr("💰 Сумма", "💰 Amount"),
             _money(order.amount) + " " + order.currency_code),
        _row(tr("💳 Способ", "💳 Method"), order.payment_method_name),
        "</table>",
        "<blockquote>" + tr(
            "Перед закрытием проверьте банк: покупатель мог перевести деньги "
            "без чека. Старые реквизиты станут недействительны, покупатель "
            "получит уведомление.",
            "Verify your bank first: the buyer may have paid without a "
            "receipt. The old instructions will be invalidated and the "
            "buyer notified.",
        ) + "</blockquote>",
    ])


def rich_file_review_result(order, *, approve: bool) -> str:
    return "\n".join([
        "<h3>" + (
            tr("✅ Оплата файла подтверждена", "✅ File payment approved")
            if approve else tr("❌ Оплата файла отклонена", "❌ File payment rejected")
        ) + "</h3>",
        "<table bordered compact>",
        _row(tr("🏷 Название", "🏷 Name"), order.product_name),
        _row(tr("💰 Сумма", "💰 Amount"),
             _money(order.amount) + " " + order.currency_code),
        _row(tr("👤 Покупатель", "👤 Customer"), order.telegram_user_id),
        _row(tr("Статус", "Status"), tr("Подтверждено", "Approved")
             if approve else tr("Отклонено", "Rejected")),
        "</table>",
        "<p>" + (
            tr("Выдача файлов осуществляется существующим механизмом доставки.",
               "Files are delivered by the existing delivery process.")
            if approve else tr("Заявка закрыта без выдачи файлов.",
                               "The request is closed without file delivery.")
        ) + "</p>",
    ])


async def send_payment_review(
    bot: Bot, *, chat_id: int, rich_html: str, legacy_text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    reply_parameters: ReplyParameters | None = None,
    parse_mode: str | None = None,
) -> Message | None:
    """Rich send with exact keyboard and receipt reply linkage or classic fallback.

    Only known API rejections trigger a classic resend. Network failures
    propagate so an uncertain delivery is never silently duplicated.
    """
    sender = getattr(bot, "send_rich_message", None)
    if (
        rich_enabled() and InputRichMessage is not None
        and inspect.iscoroutinefunction(sender)
        and len(rich_html.encode("utf-8")) < 32768
    ):
        options = {"chat_id": chat_id,
                   "rich_message": InputRichMessage(html=rich_html)}
        if reply_markup is not None:
            options["reply_markup"] = reply_markup
        if reply_parameters is not None:
            options["reply_parameters"] = reply_parameters
        try:
            return await sender(**options)
        except TelegramBadRequest as exc:
            reason = str(exc).lower()
            if not any(s in reason for s in (
                "rich", "can't parse", "unsupported", "invalid html",
                "reply_parameters",
            )):
                raise
            log.info("Rich payment review rejected; classic HTML fallback")
        except TelegramNotFound as exc:
            if "method not found" not in str(exc).lower():
                raise
            log.info("Rich payment review endpoint unavailable; classic fallback")
    options = {"chat_id": chat_id, "text": legacy_text}
    if reply_markup is not None:
        options["reply_markup"] = reply_markup
    if reply_parameters is not None:
        options["reply_parameters"] = reply_parameters
    if parse_mode is not None:
        options["parse_mode"] = parse_mode
    with classic_messages():
        return await bot.send_message(**options)
