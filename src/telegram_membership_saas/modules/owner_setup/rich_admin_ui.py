"""Read-only Rich cards for connected-bot administrators.

The tenant-scoped service and every action keyboard remain unchanged. Rendering
never mutates payments, invoices, subscriptions, or access rights.
"""
from __future__ import annotations

import inspect
import logging
from decimal import Decimal
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.types import InlineKeyboardMarkup

from telegram_membership_saas.localization import tr
from telegram_membership_saas.telegram_dates import native_telegram_time
from telegram_membership_saas.modules.owner_setup.statistics import OwnerStatistics
from telegram_membership_saas.modules.owner_setup.subscribers import (
    SubscriberDetail,
    SubscriberInvoicePage,
)
from telegram_membership_saas.modules.telegram_commerce.service import format_amount
from telegram_membership_saas.rich_messages import (
    InputRichMessage,
    classic_messages,
    rich_enabled,
)

log = logging.getLogger(__name__)


def _money(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _safe(value: object) -> str:
    return escape(str(value), quote=True)


def _row(label: str, value: str, *, native_html: bool = False) -> str:
    """Labels are trusted markup; only explicitly built date markup is trusted."""
    return (
        "<tr><th>" + label + "</th><td>"
        + (value if native_html else _safe(value)) + "</td></tr>"
    )


def _table_start(col1: str, col2: str) -> list[str]:
    return [
        "<table bordered compact>",
        "<tr><th>" + col1 + "</th><th>" + col2 + "</th></tr>",
    ]


def _customer_date(value, timezone: str, *, fmt: str = "d") -> str:
    if value is None:
        return tr("бессрочно", "lifetime")
    return native_telegram_time(value, timezone, fmt=fmt)


def rich_owner_statistics(stats: OwnerStatistics) -> str:
    """Owner project figures as explicit tables, never converted across currencies."""
    bounds = (
        f"{stats.since:%d.%m.%Y} — {stats.through:%d.%m.%Y}"
        if stats.since is not None else tr("За всё время", "All time")
    )
    body = [
        "<h3>" + tr("📊 Статистика · ViMi Pay", "📊 Statistics · ViMi Pay") + "</h3>",
        "<p><b>" + _safe(stats.project_name) + "</b><br>"
        + "🌍 " + _safe(stats.timezone) + "<br>"
        + "🗓️ " + _safe(bounds) + "</p>",
        "<p><b>" + tr("💰 Подтверждённые оплаты", "💰 Confirmed payments")
        + "</b></p>",
        "<table bordered compact>",
        _row(tr("Платежей", "Payments"), str(stats.payment_count)),
        "</table>",
    ]
    if stats.currency_totals:
        body.extend([
            "<table bordered compact>",
            "<tr><th>" + tr("Валюта", "Currency") + "</th><th>"
            + tr("Сумма", "Amount") + "</th><th>"
            + tr("Операций", "Payments") + "</th></tr>",
        ])
        for currency, amount, count in stats.currency_totals:
            body.append(
                "<tr><td>" + _safe(currency) + "</td><td>" + _safe(_money(amount))
                + "</td><td>" + str(count) + "</td></tr>"
            )
        body.append("</table>")
    else:
        body.append("<p>" + tr("Пока нет оплат", "No confirmed payments yet") + "</p>")
    body.extend([
        "<p><b>" + tr("👥 Покупатели", "👥 Customers") + "</b></p>",
        "<table bordered compact>",
        _row(tr("Новых за период", "New in period"), str(stats.new_customers)),
        _row(tr("Всего зарегистрировано", "Total registered"), str(stats.total_customers)),
        "</table>",
        "<p><b>" + tr("🔹 Сейчас", "🔹 Current status") + "</b></p>",
        "<table bordered compact>",
        _row(tr("Активных платных подписчиков", "Active paying subscribers"),
             str(stats.active_paid_customers)),
        _row(tr("Истекают за 7 дней", "Expiring within 7 days"),
             str(stats.expiring_paid_customers)),
        _row(tr("Активных ручных прав", "Active manual grants"),
             str(stats.active_manual_grants)),
        _row(tr("Заявок на проверке", "Pending payment reviews"),
             str(stats.waiting_review)),
        "</table>",
        "<p><i>" + tr(
            "Суммы не конвертируются. Оплаты подтверждены владельцем, "
            "не банком или платформой.",
            "Currencies are not converted. Payments are owner-confirmed, "
            "not independently bank-verified.",
        ) + "</i></p>",
    ])
    return "\n".join(body)


def rich_subscriber_card(data: SubscriberDetail, *, who: str) -> str:
    """Read-only customer card with separated subscriptions and grant sources."""
    lines = [
        "<h3>" + tr("👤 Карточка покупателя", "👤 Customer card") + "</h3>",
        "<p><b>" + _safe(who) + "</b></p>",
        "<table bordered compact>",
        _row("Telegram ID", str(data.telegram_id)),
        _row(tr("Блокировка", "Blocked"),
             tr("🚫 Да", "🚫 Yes") if data.is_banned else tr("✅ Нет", "✅ No")),
        _row(tr("Счетов", "Invoices"), str(data.total_invoices)),
        _row(tr("Незавершённых", "Unfinished"), str(data.pending_invoices)),
        "</table>",
        "<p><b>" + tr("📦 Подписки", "📦 Subscriptions") + "</b></p>",
    ]
    if data.subscriptions:
        lines.extend(_table_start(tr("Тариф", "Plan"), tr("Статус / до", "Status / until")))
        for sub in data.subscriptions[:8]:
            label = {
                "active": tr("активна", "active"),
                "expired": tr("истекла", "expired"),
                "cancelled": tr("отменена", "cancelled"),
            }.get(sub.status, sub.status)
            lines.append(_row(
                _safe(sub.plan_name[:45]),
                _safe(label) + " · " + _customer_date(
                    sub.period_end, data.project_timezone, fmt="dt",
                ),
                native_html=True,
            ))
        lines.append("</table>")
    else:
        lines.append("<p>" + tr("Нет покупок по тарифам", "No plan subscriptions") + "</p>")

    lines.append("<p><b>" + tr(
        "💳 Оплаченные покупки (последние 5)",
        "💳 Confirmed purchases (latest 5)",
    ) + " · " + str(data.total_payments) + "</b></p>")
    if data.payments:
        lines.extend(_table_start(tr("Тариф", "Plan"), tr("Сумма / дата", "Amount / date")))
        for paid in data.payments[:5]:
            lines.append(_row(
                _safe(paid.plan_name[:28]),
                _safe(format_amount(Decimal(paid.amount), 18) + " " + paid.currency)
                + " · " + _customer_date(
                    paid.approved_at, data.project_timezone,
                ),
                native_html=True,
            ))
        lines.append("</table>")
    else:
        lines.append("<p>" + tr("Нет подтверждённых оплат", "No confirmed payments") + "</p>")

    lines.append("<p><b>" + tr("🔑 Основания доступа", "🔑 Access grants")
                 + " · " + str(data.total_grants) + "</b></p>")
    if data.grants:
        lines.extend(_table_start(tr("Ресурс", "Resource"), tr("Основание / до", "Source / until")))
        for grant in data.grants[:8]:
            source = {
                "payment": tr("Оплата", "Paid"),
                "manual": tr("Ручной", "Manual"),
                "owner_service": tr("Служебный", "Service"),
            }.get(grant.source, grant.source)
            lines.append(_row(
                _safe(grant.resource_name[:35]),
                _safe(source) + " · " + _customer_date(
                    grant.valid_until, data.project_timezone, fmt="dt",
                ),
                native_html=True,
            ))
        lines.append("</table>")
    else:
        lines.append("<p>" + tr("Нет действующих прав", "No active grants") + "</p>")

    if data.access:
        lines.extend([
            "<p><b>" + tr("📣 Доступ к каналам", "📣 Channel access") + "</b></p>",
            *_table_start(tr("Ресурс", "Resource"), tr("Доступ", "Access")),
        ])
        for entry in data.access[:8]:
            mark = "✅" if entry.actual == "granted" else "⏳" if entry.desired else "⚪"
            label = {
                "granted": tr("выдан", "granted"),
                "revoked": tr("отозван", "revoked"),
                "unknown": tr("не подтверждён", "not verified"),
            }.get(entry.actual, entry.actual)
            lines.append(_row(_safe(entry.resource_name[:35]), mark + " " + label))
        lines.append("</table>")
    return "\n".join(lines)


def rich_subscriber_invoices(data: SubscriberInvoicePage, *, who: str) -> str:
    """Eight invoices per page with original status, amount and local date."""
    pages = max(1, (data.total + 7) // 8)
    lines = [
        "<h3>" + tr("🧾 Счета покупателя", "🧾 Customer invoices") + "</h3>",
        "<p>👤 " + _safe(who) + " · ID " + str(data.telegram_id) + "<br>"
        + tr("Всего счетов: ", "Total invoices: ") + str(data.total) + "<br>"
        + tr("Страница ", "Page ") + str(data.page + 1) + " / " + str(pages) + "</p>",
    ]
    if not data.invoices:
        lines.append("<p>" + tr("Счетов пока нет.", "No invoices yet.") + "</p>")
        return "\n".join(lines)
    lines.extend([
        "<table bordered compact>",
        "<tr><th>№</th><th>" + tr("Тариф", "Plan") + "</th><th>"
        + tr("Сумма / статус", "Amount / status") + "</th></tr>",
    ])
    for invoice in data.invoices[:8]:
        icon = {
            "waiting_confirmation": "⏳",
            "waiting_review": "🕒",
            "approved": "✅",
            "rejected": "❌",
            "cancelled": "⚪",
            "expired": "⌛",
        }.get(invoice.status, "📄")
        status = {
            "waiting_confirmation": tr("Ожидает оплату", "Awaiting payment"),
            "waiting_review": tr("На проверке", "Under review"),
            "approved": tr("Подтверждён", "Approved"),
            "rejected": tr("Отклонён", "Rejected"),
            "cancelled": tr("Отменён", "Cancelled"),
            "expired": tr("Истёк", "Expired"),
        }.get(invoice.status, invoice.status)
        amount = format_amount(Decimal(invoice.amount), 18)
        lines.append(
            "<tr><td>" + icon + " " + invoice.id.hex[:10].upper()
            + "</td><td>" + _safe(invoice.plan_name[:30]) + "</td><td>"
            + _safe(amount + " " + invoice.currency) + "<br>"
            + _safe(status) + "<br>"
            + _customer_date(invoice.created_at, data.project_timezone)
            + "</td></tr>"
        )
    lines.append("</table>")
    lines.append("<p>" + tr(
        "👇 Нажмите на счёт ниже для подробностей.",
        "👇 Select an invoice below for details.",
    ) + "</p>")
    return "\n".join(lines)


async def send_owner_rich(
    bot: Bot, *, chat_id: int, rich_html: str, classic_html: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    """Use explicit Rich tables or unchanged classic HTML on unsupported API.

    Do not retry on transport errors: an admin message may already be delivered.
    """
    sender = getattr(bot, "send_rich_message", None)
    if (
        rich_enabled() and InputRichMessage is not None
        and inspect.iscoroutinefunction(sender)
        and len(rich_html.encode("utf-8")) < 32768
    ):
        try:
            await sender(
                chat_id=chat_id, rich_message=InputRichMessage(html=rich_html),
                reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest as exc:
            reason = str(exc).lower()
            if not any(token in reason for token in (
                "rich", "can't parse", "unsupported", "invalid html",
            )):
                raise
            log.info("Rich owner screen unavailable, using classic HTML")
        except TelegramNotFound as exc:
            if "method not found" not in str(exc).lower():
                raise
            log.info("sendRichMessage unavailable, using classic HTML")
    with classic_messages():
        await bot.send_message(
            chat_id, classic_html, parse_mode="HTML", reply_markup=reply_markup,
        )
