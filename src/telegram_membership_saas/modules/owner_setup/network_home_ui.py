"""Compact bilingual main dashboard, Rich and classic Telegram HTML.

Every number is backed by the owner-scoped snapshot. Do not replace an unknown
balance with zero. This is gross confirmed native sales, NOT withdrawal balance
or profit; channel-native Stars receipts are not observable via the bot API.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo

from telegram_membership_saas.currency_display import display_currency
from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER

_HOME_TZ = ZoneInfo("Europe/Moscow")
_SYMBOL = {"RUB": "₽", "USD": "$", "EUR": "€", "UAH": "₴", "BYN": "Br",
           "XTR": "⭐"}


def _number(raw: object) -> str:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid fleet money amount") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("Invalid nonnegative fleet amount")
    if value == value.to_integral_value():
        rendered = format(value, ".0f")
    else:
        rendered = format(value, "f").rstrip("0").rstrip(".")
    head, dot, fraction = rendered.partition(".")
    groups = []
    while head:
        groups.insert(0, head[-3:])
        head = head[:-3]
    return " ".join(groups) + (dot + fraction if dot else "")


def _price(amount: object, currency: str) -> str:
    code = str(currency)
    formatted = _number(amount)
    if code == "XTR":
        return formatted + " ⭐"
    return formatted + " " + _SYMBOL.get(code, escape(code, quote=True))


def _due_date(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("No authenticated due timestamp")
    instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Unzoned due timestamp")
    return instant.astimezone(_HOME_TZ).strftime("%d.%m.%Y")


def _labels(report: dict) -> dict:
    fleet, bill = report["network"], report["billing"]
    current = bill.get("next")
    if current is not None and not isinstance(current, dict):
        raise ValueError("Invalid nearest invoice")
    sales = []
    for item in report["revenue"]:
        currency = str(item["currency"])
        amount = Decimal(str(item["gross"]))
        if amount <= 0:
            continue
        sales.append((
            escape(display_currency(currency), quote=True),
            _price(amount, currency),
        ))
    nearest = None
    if current:
        next_user = current.get("username")
        if next_user:
            identity = "🤖 @" + escape(str(next_user).lstrip("@"), quote=True)
        else:
            identity = tr("🤖 Ранее отключённый бот",
                          "🤖 Previously disconnected bot")
        nearest = {
            "identity": identity,
            "date": _due_date(current["due_at"]),
            "amount": _price(current["remaining_rub"], "RUB"),
            "same_day_extra": max(0, int(current["same_day_count"]) - 1),
        }
    return {
        "connected": int(fleet["connected"]),
        "healthy": int(fleet["healthy"]),
        "attention": int(fleet["attention"]),
        "down": int(fleet["down"]),
        "sales": sales,
        "unpaid": int(bill["unpaid"]),
        "overdue": int(bill["overdue"]),
        "overdue_amount": _price(bill["overdue_rub"], "RUB"),
        "next": nearest,
    }


def _sections(report: dict, *, rich: bool) -> str:
    data = _labels(report)
    lead = tr(
        "Управляйте подключёнными Telegram-ботами, обслуживанием "
        "и счетами в одном месте.",
        "Manage connected Telegram bots, service and account invoices "
        "in one place.",
    )
    if rich:
        sections = [
            "<h3>✦ ViMi Pay · " + tr("ГЛАВНОЕ МЕНЮ", "MAIN MENU") + "</h3>",
            "<p><b>" + tr("👋 ДОБРО ПОЖАЛОВАТЬ В ViMi Pay",
                          "👋 WELCOME TO ViMi Pay") + "</b><br><i>"
            + lead + "</i></p>",
            "<h4>" + tr("📊 ВАША СЕТЬ", "📊 YOUR NETWORK") + "</h4>",
            "<table bordered compact>",
            "<tr><th>🤖 " + tr("Подключено ботов", "Connected bots")
            + "</th><td><b>" + str(data["connected"]) + "</b></td></tr>",
            "<tr><th>🟢 " + tr("Работают корректно", "Healthy")
            + "</th><td><b>" + str(data["healthy"]) + "</b></td></tr>",
            "<tr><th>🟡 " + tr("Требуют внимания", "Need attention")
            + "</th><td><b>" + str(data["attention"]) + "</b></td></tr>",
            "<tr><th>🔴 " + tr("Ошибки / не работают", "Down / errors")
            + "</th><td><b>" + str(data["down"]) + "</b></td></tr>",
            "</table>",
            "<h4>" + tr("💰 ВЫРУЧКА ВСЕЙ СЕТИ", "💰 NETWORK SALES") + "</h4>",
            "<p><i>" + tr(
                "Подтверждённые продажи за всё время · исходные валюты",
                "All-time confirmed sales · original currencies",
            ) + "</i></p>",
        ]
        if data["sales"]:
            sections.append("<table bordered compact>")
            for currency, amount in data["sales"]:
                sections.append(
                    "<tr><th>" + currency + "</th><td><b>"
                    + amount + "</b></td></tr>"
                )
            sections.append("</table>")
        else:
            sections.append("<p><i>" + tr(
                "Подтверждённых продаж пока нет.",
                "No confirmed sales yet.",
            ) + "</i></p>")
        sections.extend([
            "<p><i>" + tr(
                "Суммы до возвратов и комиссий; это не баланс для вывода. "
                "Продажи напрямую внутри Telegram-канала без данных бота "
                "не включены.",
                "Gross amounts before refunds and fees, not withdrawable balances. "
                "Direct channel-only Telegram sales without bot records are excluded.",
            ) + "</i></p>",
            "<h4>" + tr("🧾 РАСЧЁТЫ С ViMi Pay",
                         "🧾 ViMi Pay BILLING") + "</h4>",
            "<table bordered compact>",
            "<tr><th>" + tr("📋 Счетов к оплате", "📋 Unpaid invoices")
            + "</th><td><b>" + str(data["unpaid"]) + "</b></td></tr>",
            "<tr><th>" + tr("🔴 Просрочено", "🔴 Overdue")
            + "</th><td>" + (
                "<b>" + str(data["overdue"]) + " · "
                + data["overdue_amount"] + "</b>"
                if data["overdue"] else tr("✅ Нет", "✅ None")
            ) + "</td></tr>",
            "</table>",
        ])
        if data["next"]:
            future = data["next"]
            sections.extend([
                "<p><b>" + tr("📅 Ближайший платёж", "📅 Next payment")
                + "</b><br>" + future["identity"] + " · <b>"
                + future["date"] + "</b> · <b>" + future["amount"]
                + "</b></p>",
            ])
            if future["same_day_extra"]:
                sections.append("<p><i>+ " + tr(
                    "ещё", "another",
                ) + " " + str(future["same_day_extra"]) + " · "
                + tr("смотреть в «Счёт аккаунта»",
                     "see Account bill") + "</i></p>")
        sections.extend([
            "<h4>" + tr("🚀 УПРАВЛЕНИЕ", "🚀 MANAGEMENT") + "</h4>",
            RICH_ACTIONS_MARKER,
        ])
        return "\n".join(sections)

    lines = [
        "✦ <b>ViMi Pay · " + tr("ГЛАВНОЕ МЕНЮ", "MAIN MENU") + "</b>",
        "<b>" + tr("👋 ДОБРО ПОЖАЛОВАТЬ В ViMi Pay",
                    "👋 WELCOME TO ViMi Pay") + "</b>",
        "<i>" + lead + "</i>",
        "",
        tr("📊 <b>ВАША СЕТЬ</b>", "📊 <b>YOUR NETWORK</b>"),
        "🤖 " + tr("Подключено ботов", "Connected bots") + ": <b>"
        + str(data["connected"]) + "</b>",
        "🟢 " + tr("Работают корректно", "Healthy") + ": <b>"
        + str(data["healthy"]) + "</b>",
        "🟡 " + tr("Требуют внимания", "Need attention") + ": <b>"
        + str(data["attention"]) + "</b>",
        "🔴 " + tr("Ошибки / не работают", "Down / errors") + ": <b>"
        + str(data["down"]) + "</b>",
        "",
        tr("💰 <b>ВЫРУЧКА ВСЕЙ СЕТИ</b>",
           "💰 <b>NETWORK SALES</b>"),
    ]
    if data["sales"]:
        lines.extend(currency + ": <b>" + money + "</b>"
                     for currency, money in data["sales"])
    else:
        lines.append(tr("Подтверждённых продаж пока нет.",
                        "No confirmed sales yet."))
    lines.extend([
        "<i>" + tr(
            "За всё время, до возвратов и комиссий. Не баланс для вывода. "
            "Продажи напрямую внутри каналов без данных бота не включены.",
            "All time, before refunds and fees; not a withdrawal balance. "
            "Channel-only sales without bot data are excluded.",
        ) + "</i>",
        "",
        tr("🧾 <b>РАСЧЁТЫ С ViMi Pay</b>",
           "🧾 <b>ViMi Pay BILLING</b>"),
        tr("📋 Счетов к оплате", "📋 Unpaid invoices") + ": <b>"
        + str(data["unpaid"]) + "</b>",
        tr("🔴 Просрочено", "🔴 Overdue") + ": <b>"
        + (
            str(data["overdue"]) + " · " + data["overdue_amount"]
            if data["overdue"] else tr("Нет", "None")
        ) + "</b>",
    ])
    if data["next"]:
        future = data["next"]
        lines.extend([
            "📅 <b>" + tr("Ближайший платёж", "Next payment") + "</b>",
            future["identity"] + " · <b>" + future["date"]
            + "</b> · <b>" + future["amount"] + "</b>",
        ])
        if future["same_day_extra"]:
            lines.append("+ " + tr("ещё", "another")
                         + " " + str(future["same_day_extra"])
                         + " · " + tr("Счёт аккаунта", "Account bill"))
    lines.extend(["", tr("🚀 <b>УПРАВЛЕНИЕ</b>", "🚀 <b>MANAGEMENT</b>")])
    return "\n".join(lines)


def owner_network_home_rich(report: dict) -> str:
    return _sections(report, rich=True)


def owner_network_home_classic(report: dict) -> str:
    return _sections(report, rich=False)
