"""One bilingual Rich ViMi Pay bill, with an HTML fallback.

Only completed sales appear as gross turnover. A provisional 5% charge is
NOT a payable invoice until the closed-month finance ledger issues it.
"""
from __future__ import annotations

from decimal import Decimal
from html import escape

from telegram_membership_saas.currency_display import display_currency
from telegram_membership_saas.localization import tr


def _money(value: object) -> str:
    return escape(format(Decimal(str(value)), ".2f")) + " ₽"


def _native_amount(value: object, currency: str) -> str:
    amount = Decimal(str(value))
    number = format(amount, "f").rstrip("0").rstrip(".")
    if "." not in format(amount, "f"):
        number = format(amount, "f")
    return escape(number + " " + display_currency(currency))


def _month(value: object) -> str:
    token = str(value)[:7]
    try:
        year, month = map(int, token.split("-"))
        if not 1 <= month <= 12:
            raise ValueError("month")
        ru = (
            "январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
        )
        en = (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        )
        return tr(ru[month - 1], en[month - 1]) + " " + str(year)
    except (TypeError, ValueError):
        return escape(token)


def _sales(snapshot: dict, preview: dict, *, category: str) -> tuple[list[dict], int, Decimal]:
    rows = [
        row for row in snapshot["turnover"] if row["kind"] == category
    ]
    count = sum(int(row["sold"]) for row in rows)
    kinds = (
        ("subscription", "subscription_stars", "subscription_crypto")
        if category == "subscription"
        else ("file_manual", "file_stars", "file_crypto")
    )
    fee = sum(
        (Decimal(str(rail["commission_rub"]))
         for rail in preview["rails"] if rail["kind"] in kinds),
        Decimal("0"),
    )
    return rows, count, fee


def _sales_table(snapshot: dict, preview: dict, category: str) -> list[str]:
    rows, count, fee = _sales(snapshot, preview, category=category)
    heading = (
        tr("📣 Подписки", "📣 Subscriptions")
        if category == "subscription"
        else tr("📁 Файлы", "📁 Files")
    )
    html = ["<h4>" + heading + "</h4>"]
    if rows:
        html.extend([
            "<table bordered compact>",
            "<tr><th>" + tr("Валюта", "Currency") + "</th><th>"
            + tr("Оборот", "Gross sales") + "</th><th>"
            + tr("Продано", "Sold") + "</th></tr>",
        ])
        for row in rows:
            currency = str(row["currency"])
            html.append(
                "<tr><td>" + escape(display_currency(currency))
                + "</td><td>" + _native_amount(row["gross"], currency)
                + "</td><td>" + str(int(row["sold"])) + "</td></tr>"
            )
        html.append("</table>")
    else:
        html.append("<p>" + tr(
            "Продаж пока нет.", "No completed sales yet.",
        ) + "</p>")
    html.append(
        "<p>" + tr("Продано: ", "Sold: ") + "<b>" + str(count)
        + "</b> · " + tr("5% начислено: ", "5% accrued: ")
        + "<b>" + _money(fee) + "</b></p>"
    )
    return html


def unified_merchant_bill_rich(snapshot: dict, preview: dict) -> str:
    """Compact single view, no separate invoice buttons or ledger IDs."""
    service = Decimal(str(snapshot["service_due_rub"]))
    commission = Decimal(str(snapshot["commission_due_rub"]))
    due = Decimal(str(snapshot["total_due_rub"]))
    if due != service + commission:
        raise ValueError("Inconsistent ViMi Pay payable breakdown")
    lines = [
        "<h3>" + tr("🧾 Счёт ViMi Pay", "🧾 ViMi Pay bill") + "</h3>",
        "<p>" + _month(snapshot["month"]) + " · "
        + (
            tr("продажи этого бота", "sales for this bot")
            if snapshot.get("project_id") else
            tr("продажи аккаунта", "account sales")
        ) + "</p>",
        "<h4>" + tr("🤖 Обслуживание", "🤖 Service") + "</h4>",
        "<p>" + tr("Остаток за обслуживание по аккаунту: ",
                  "Account service balance: ")
        + "<b>" + _money(service) + "</b></p>",
        *_sales_table(snapshot, preview, "subscription"),
        *_sales_table(snapshot, preview, "file"),
        "<h4>" + tr("💰 К оплате по аккаунту", "💰 Account balance due")
        + "</h4>",
        "<table bordered compact>",
        "<tr><td>" + tr("Обслуживание", "Service") + "</td><td>"
        + _money(service) + "</td></tr>",
        "<tr><td>" + tr("Выставленная комиссия", "Invoiced commission")
        + "</td><td>" + _money(commission) + "</td></tr>",
        "<tr><td><b>" + tr("Итого к оплате", "Total payable")
        + "</b></td><td><b>" + _money(due) + "</b></td></tr>",
        "</table>",
        "<p><i>" + tr(
            "Комиссия 5% за текущий месяц показана выше как начисление "
            "и войдёт в оплату только после закрытия месяца. "
            "Обслуживание и старые долги уже включены в остаток.",
            "Current-month 5% is shown above as accrual, not yet payable. "
            "Service and previously invoiced balances are already due.",
        ) + "</i></p>",
    ]
    if int(preview.get("unrated_billable", 0)) or int(
        preview.get("uncaptured_crypto", 0)
    ):
        lines.append("<p>⚠️ " + tr(
            "Некоторые продажи ещё проходят сверку валют и Crypto Pay. "
            "Их комиссия не включена в подтверждённый расчёт.",
            "Some sale-date FX and Crypto Pay evidence is unresolved; "
            "the provisional commission is incomplete.",
        ) + "</p>")
    return "\n".join(lines)


def unified_merchant_bill_classic(snapshot: dict, preview: dict) -> str:
    """Telegram fallback without Rich-only table tags."""
    service = _money(snapshot["service_due_rub"])
    commission = _money(snapshot["commission_due_rub"])
    due = _money(snapshot["total_due_rub"])
    lines = [
        tr("🧾 <b>Счёт ViMi Pay</b>", "🧾 <b>ViMi Pay bill</b>"),
        _month(snapshot["month"]),
        "",
        tr("🤖 <b>Обслуживание</b>", "🤖 <b>Service</b>"),
        tr("По аккаунту: ", "Account due: ") + service,
    ]
    for category in ("subscription", "file"):
        rows, count, fee = _sales(snapshot, preview, category=category)
        lines.extend([
            "",
            (tr("📣 <b>Подписки</b>", "📣 <b>Subscriptions</b>")
             if category == "subscription"
             else tr("📁 <b>Файлы</b>", "📁 <b>Files</b>")),
        ])
        for row in rows:
            lines.append(
                "• " + _native_amount(row["gross"], str(row["currency"]))
                + " · " + str(int(row["sold"]))
            )
        lines.append(
            tr("Продано: ", "Sold: ") + str(count) + " · 5%: " + _money(fee)
        )
    lines.extend([
        "",
        tr("💰 <b>К оплате по аккаунту</b>",
           "💰 <b>Account balance due</b>"),
        tr("Обслуживание: ", "Service: ") + service,
        tr("Выставленная комиссия: ", "Invoiced commission: ") + commission,
        tr("<b>Итого: ", "<b>Total: ") + due + "</b>",
        "",
        "<i>" + tr(
            "Комиссия текущего месяца предварительная и пока не входит "
            "в сумму к оплате.",
            "Current-month commission is provisional and is not payable yet.",
        ) + "</i>",
    ])
    if int(preview.get("unrated_billable", 0)) or int(
        preview.get("uncaptured_crypto", 0)
    ):
        lines.append("⚠️ " + tr(
            "Некоторые продажи ожидают сверки.",
            "Some sales are awaiting reconciliation.",
        ))
    return "\n".join(lines)
