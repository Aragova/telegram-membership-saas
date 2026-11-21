"""Approved account-wide ViMi Pay billing overview, RU/EN Rich and classic.

All amounts, counts, and the nearest issued future invoice come from the
existing owner-authorized read-only finance reports. No synthetic invoices.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from html import escape

from telegram_membership_saas.localization import tr


def _money(value: object) -> str:
    amount = Decimal(str(value))
    number = format(amount, ".2f")
    if number.endswith(".00"):
        number = number[:-3]
    return escape(number.replace(".", ","), quote=True) + " ₽"


def _date(value: object) -> str:
    raw = str(value or "")[:10]
    try:
        return date.fromisoformat(raw).strftime("%d.%m.%Y")
    except ValueError:
        return tr("Дата уточняется", "Date unavailable")


def _labels(statement, billing: dict) -> dict:
    overdue = max(0, int(billing["overdue"]))
    unpaid = max(0, int(statement.unpaid))
    nearest = billing.get("next") if unpaid else None
    if nearest is not None and not isinstance(nearest, dict):
        nearest = None
    return {
        "unpaid": unpaid,
        "overdue": overdue,
        "overdue_rub": _money(billing["overdue_rub"]),
        "due_rub": _money(statement.outstanding_rub),
        "nearest": nearest,
    }


def account_bill_rich(statement, billing: dict) -> str:
    data = _labels(statement, billing)
    lines = [
        "<h3>" + tr("🧾 СЧЁТ АККАУНТА", "🧾 ACCOUNT BILL") + "</h3>",
        "<h4>" + tr("💼 ВАШИ РАСЧЁТЫ С ViMi Pay",
                    "💼 YOUR ViMi Pay BILLING") + "</h4>",
        "<p><i>" + tr(
            "Все счета за обслуживание и комиссию по вашим ботам в одном месте.",
            "All bot service and commission invoices in one place.",
        ) + "</i></p>",
    ]
    if data["unpaid"]:
        lines.append("<h4>⚠️ " + tr("ЕСТЬ НЕОПЛАЧЕННЫЕ СЧЕТА",
                                    "UNPAID INVOICES") + "</h4>")
    else:
        lines.extend([
            "<h4>✅ " + tr("НЕТ СЧЕТОВ К ОПЛАТЕ",
                              "NO INVOICES TO PAY") + "</h4>",
            "<p>✅ " + tr("Сейчас задолженности перед ViMi Pay нет.",
                           "You have no outstanding ViMi Pay debt.")
            + "</p>",
        ])
    lines.extend([
        "<h4>" + tr("📊 ФИНАНСОВАЯ СВОДКА",
                    "📊 FINANCIAL SUMMARY") + "</h4>",
        "<table bordered compact>",
        "<tr><th>" + tr("📋 Неоплаченных счетов", "📋 Unpaid invoices")
        + "</th><td><b>" + str(data["unpaid"]) + "</b></td></tr>",
        "<tr><th>" + tr("⏰ Просрочено", "⏰ Overdue") + "</th><td><b>"
        + (str(data["overdue"]) + " · " + data["overdue_rub"]
           if data["overdue"] else tr("Нет", "None")) + "</b></td></tr>",
        "</table>",
        "<p>💰 " + tr("ВСЕГО К ОПЛАТЕ", "TOTAL AMOUNT DUE") + "<br>"
        + "<b>" + data["due_rub"] + "</b></p>",
    ])
    next_due = data["nearest"]
    if next_due:
        name = next_due.get("username")
        identity = ("🤖 @" + escape(str(name).lstrip("@"), quote=True)
                    if name else tr("🤖 Ранее отключённый бот",
                                    "🤖 Previously disconnected bot"))
        lines.extend([
            "<h4>" + tr("📅 БЛИЖАЙШИЙ ПЛАТЁЖ",
                         "📅 NEXT PAYMENT") + "</h4>",
            "<table bordered compact>",
            "<tr><th>" + tr("Бот", "Bot") + "</th><td>" + identity
            + "</td></tr>",
            "<tr><th>" + tr("Сумма", "Amount") + "</th><td><b>"
            + _money(next_due["remaining_rub"]) + "</b></td></tr>",
            "<tr><th>" + tr("Срок оплаты", "Pay by") + "</th><td><b>"
            + _date(next_due["due_at"]) + "</b></td></tr>",
            "</table>",
        ])
        extra = max(0, int(next_due.get("same_day_count") or 0) - 1)
        if extra:
            lines.append("<p><i>+ " + tr(
                "ещё ", "another ") + str(extra) + tr(
                " неоплаченных счетов с этим сроком",
                " unpaid invoices with the same due date",
            ) + "</i></p>")
    elif data["overdue"]:
        lines.append("<p>⚠️ " + tr(
            "Проверьте просроченные счета в списке выставленных счетов.",
            "Check overdue invoices in the issued invoice list.",
        ) + "</p>")
    lines.append("<p><i>ℹ️ " + tr(
        "Каждый бот имеет собственный счёт. Здесь показаны общие "
        "обязательства всего аккаунта.",
        "Each bot has its own bill. This screen summarizes obligations "
        "across your account.",
    ) + "</i></p>")
    return "\n".join(lines)


def account_bill_classic(statement, billing: dict) -> str:
    data = _labels(statement, billing)
    parts = [
        tr("🧾 <b>СЧЁТ АККАУНТА</b>", "🧾 <b>ACCOUNT BILL</b>"),
        "<b>" + tr("💼 ВАШИ РАСЧЁТЫ С ViMi Pay",
                    "💼 YOUR ViMi Pay BILLING") + "</b>",
        "<i>" + tr(
            "Все счета за обслуживание и комиссию по вашим ботам в одном месте.",
            "All bot service and commission invoices in one place.",
        ) + "</i>",
        ("⚠️ <b>" + tr("ЕСТЬ НЕОПЛАЧЕННЫЕ СЧЕТА",
                        "UNPAID INVOICES") + "</b>"
         if data["unpaid"] else "✅ <b>" + tr(
            "НЕТ СЧЕТОВ К ОПЛАТЕ", "NO INVOICES TO PAY") + "</b>\n"
         + tr("Сейчас задолженности перед ViMi Pay нет.",
              "You have no outstanding ViMi Pay debt.")),
        "<b>" + tr("📊 ФИНАНСОВАЯ СВОДКА",
                    "📊 FINANCIAL SUMMARY") + "</b>\n"
        + tr("📋 Неоплаченных счетов: ", "📋 Unpaid invoices: ")
        + "<b>" + str(data["unpaid"]) + "</b>\n"
        + tr("⏰ Просрочено: ", "⏰ Overdue: ") + "<b>"
        + (str(data["overdue"]) + " · " + data["overdue_rub"]
           if data["overdue"] else tr("Нет", "None")) + "</b>",
        "💰 " + tr("ВСЕГО К ОПЛАТЕ", "TOTAL AMOUNT DUE") + "\n<b>"
        + data["due_rub"] + "</b>",
    ]
    next_due = data["nearest"]
    if next_due:
        name = next_due.get("username")
        identity = ("🤖 @" + escape(str(name).lstrip("@"), quote=True)
                    if name else tr("🤖 Ранее отключённый бот",
                                    "🤖 Previously disconnected bot"))
        parts.append("<b>" + tr("📅 БЛИЖАЙШИЙ ПЛАТЁЖ",
                               "📅 NEXT PAYMENT") + "</b>\n"
                     + identity + " · " + _money(next_due["remaining_rub"])
                     + "\n" + tr("📅 Срок оплаты: ", "📅 Pay by: ")
                     + "<b>" + _date(next_due["due_at"]) + "</b>")
        extra = max(0, int(next_due.get("same_day_count") or 0) - 1)
        if extra:
            parts.append("+ " + str(extra) + tr(
                " неоплаченных счетов с этим сроком",
                " unpaid invoices with the same due date",
            ))
    elif data["overdue"]:
        parts.append("⚠️ " + tr(
            "Проверьте просроченные счета в списке.",
            "Check overdue invoices in the list.",
        ))
    parts.append("<i>ℹ️ " + tr(
        "Каждый бот имеет собственный счёт. Здесь показаны общие "
        "обязательства всего аккаунта.",
        "Each bot has its own bill. This screen summarizes obligations "
        "across your account.",
    ) + "</i>")
    return "\n\n".join(parts)
