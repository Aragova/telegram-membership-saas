"""Single per-bot ViMi Pay invoice, rendered using issued financial evidence.

The service fee is prepaid for the new month. Sales are from this bot's
PREVIOUS completed service month. First activation has no sales commission.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from telegram_membership_saas.currency_display import display_currency
from telegram_membership_saas.localization import tr


def _money(amount: object) -> str:
    return escape(format(Decimal(str(amount)), ".2f")) + " ₽"


def _native(amount: object, code: str) -> str:
    number = format(Decimal(str(amount)), "f")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return escape(number + " " + display_currency(code))


def _service_amount(bill: dict) -> str:
    if bill.get("service_currency") == "USD":
        return "$" + escape(format(Decimal(str(
            bill.get("service_native", 3)
        )), ".2f"))
    return _money(bill["service_rub"])


def _date(value: object) -> str:
    if not value:
        return ""
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("Europe/Moscow"))
        return dt.strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return escape(str(value)[:10])


def _instant(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(ZoneInfo("Europe/Moscow"))
    except (TypeError, ValueError):
        pass
    return None


def _date_time(value: object) -> str:
    moment = _instant(value)
    return moment.strftime("%d.%m.%Y %H:%M МСК") if moment else ""


def _bill_status(bill: dict) -> list[str]:
    """Actual issued/payment state, not a synthetic bill or test action."""
    due = _instant(bill.get("due_at"))
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    owed = Decimal(str(bill.get("total_due_rub", 0)))
    state = str(bill.get("hold_state") or "clear")
    result: list[str] = []
    if bill.get("invoice_id") is None:
        next_at = _date_time(bill.get("upcoming_at"))
        if next_at:
            result.append(tr("📅 Предстоящий счёт: ",
                             "📅 Upcoming invoice: ") + next_at)
        return result
    if owed == 0:
        result.append(tr("✅ Статус: оплачено", "✅ Status: paid"))
        upcoming = _date_time(bill.get("upcoming_at"))
        if upcoming:
            result.append(tr("📅 Следующий расчёт: ", "📅 Next bill: ")
                          + upcoming)
    elif due and due > now:
        result.append(tr("🟡 Статус: выставлен", "🟡 Status: issued"))
        result.append(tr("📅 Оплатить до: ", "📅 Pay by: ")
                      + _date_time(bill["due_at"]))
    else:
        result.append(tr("🔴 Статус: просрочен", "🔴 Status: overdue"))
        if due:
            result.append(tr("📅 Срок оплаты: ", "📅 Due date: ")
                          + _date_time(bill["due_at"]))
        protected = _instant(bill.get("review_until"))
        if protected and protected > now and state == "review":
            result.append(tr("⏳ Чек на проверке · защита до ",
                             "⏳ Receipt pending · protection until ")
                          + _date_time(bill["review_until"]))
    if state == "held":
        result.append(tr("⛔ Новые продажи в сети ограничены до оплаты долга.",
                         "⛔ New sales across your network are on hold."))
    return result


def _sales(bill: dict, *, kind: str) -> tuple[list[dict], int, str]:
    rows = [r for r in bill["turnover"] if r["kind"] == kind]
    count = sum(int(r["sold"]) for r in rows)
    amount = (
        bill["subscription_rub"]
        if kind == "subscription" else bill["file_rub"]
    )
    return rows, count, _money(amount)


def _validate(bill: dict) -> None:
    if bill.get("invoice_id") is None:
        return
    total = Decimal(str(bill["total_rub"]))
    paid = Decimal(str(bill["paid_rub"]))
    credit = Decimal(str(bill.get("credit_rub", 0)))
    due = Decimal(str(bill["total_due_rub"]))
    if total != paid + credit + due or any(
        value < 0 for value in (total, paid, credit, due)
    ):
        raise ValueError("Inconsistent per-bot ViMi Pay invoice totals")


def _section_rich(bill: dict, kind: str) -> list[str]:
    rows, count, fee = _sales(bill, kind=kind)
    title = (
        tr("📣 Продажи подписок", "📣 Subscription sales")
        if kind == "subscription"
        else tr("📁 Продажи файлов", "📁 File sales")
    )
    lines = ["<h4>" + title + "</h4>", "<table bordered compact>"]
    for row in rows:
        lines.append(
            "<tr><td>" + tr("Оборот", "Turnover") + "</td><td>"
            + _native(row["gross"], str(row["currency"])) + "</td></tr>"
        )
    if not rows:
        lines.append(
            "<tr><td>" + tr("Оборот", "Turnover")
            + "</td><td>0 ₽</td></tr>"
        )
    lines.extend([
        "<tr><td>" + tr("Продано", "Sold") + "</td><td>"
        + str(count) + "</td></tr>",
        "<tr><td>" + tr("Комиссия 5%", "Commission 5%")
        + "</td><td>" + fee + "</td></tr>",
        "</table>",
    ])
    return lines


def unified_merchant_bill_rich(bill: dict) -> str:
    _validate(bill)
    lines = ["<h3>" + tr("🧾 Счёт ViMi Pay", "🧾 ViMi Pay invoice") + "</h3>"]
    if bill.get("invoice_id") is None:
        lines.extend("<p>" + entry + "</p>" for entry in _bill_status(bill))
        lines.append("<p>" + tr(
            "Счёт пока не выставлен. Оплата доступна только по реальному счёту.",
            "No invoice issued yet. Pay only an actual issued invoice.",
        ) + "</p>")
        return "\n".join(lines)
    lines.extend("<p>" + entry + "</p>" for entry in _bill_status(bill))
    lines.extend([
        "<p>" + tr("Обслуживание: ", "Service period: ")
        + _date(bill.get("period_start")) + " – "
        + _date(bill.get("period_end")) + "</p>",
        "<h4>" + tr("🤖 Обслуживание", "🤖 Service") + "</h4>",
        "<p>" + tr("Абонплата за бота: ", "Bot subscription fee: ")
        + "<b>" + _service_amount(bill) + "</b></p>",
    ])
    if not bill.get("first_period", False):
        lines.extend(_section_rich(bill, "subscription"))
        lines.extend(_section_rich(bill, "file"))
    lines.extend([
        "<h4>" + tr("💰 Итог", "💰 Total") + "</h4>",
        "<p>" + tr("Всего начислено: ", "Total charged: ")
        + "<b>" + _money(bill["total_rub"]) + "</b></p>",
        "<p>" + tr("Подтверждённые оплаты: ", "Confirmed payments: ")
        + _money(bill["paid_rub"]) + "</p>",
    ])
    if Decimal(str(bill.get("credit_rub", 0))) > 0:
        lines.append(
            "<p>" + tr("Учтённые возвраты: ", "Applied refund credits: ")
            + _money(bill["credit_rub"]) + "</p>"
        )
    lines.append(
        "<h4>" + tr("Итого к оплате", "Amount due") + ": "
        + _money(bill["total_due_rub"]) + "</h4>"
    )
    return "\n".join(lines)


def unified_merchant_bill_classic(bill: dict) -> str:
    _validate(bill)
    lines = [tr("🧾 <b>Счёт ViMi Pay</b>", "🧾 <b>ViMi Pay invoice</b>")]
    if bill.get("invoice_id") is None:
        lines.extend(_bill_status(bill))
        lines.append(tr(
            "Счёт пока не выставлен.",
            "No invoice has been issued yet.",
        ))
        return "\n".join(lines)
    lines.extend(_bill_status(bill))
    lines.extend([
        tr("Период: ", "Service period: ")
        + _date(bill.get("period_start")) + " – "
        + _date(bill.get("period_end")),
        "",
        tr("🤖 <b>Обслуживание</b>", "🤖 <b>Service</b>"),
        tr("Абонплата за бота: ", "Bot fee: ")
        + "<b>" + _service_amount(bill) + "</b>",
    ])
    if not bill.get("first_period", False):
        for kind in ("subscription", "file"):
            rows, count, fee = _sales(bill, kind=kind)
            lines.extend([
                "",
                (tr("📣 <b>Продажи подписок</b>",
                    "📣 <b>Subscription sales</b>")
                 if kind == "subscription"
                 else tr("📁 <b>Продажи файлов</b>", "📁 <b>File sales</b>")),
            ])
            for row in rows:
                lines.append(tr("Оборот: ", "Turnover: ")
                             + _native(row["gross"], str(row["currency"])))
            if not rows:
                lines.append(tr("Оборот: 0 ₽", "Turnover: 0 ₽"))
            lines.extend([
                tr("Продано: ", "Sold: ") + str(count),
                tr("Комиссия 5%: ", "Commission 5%: ") + fee,
            ])
    lines.extend([
        "",
        tr("Всего начислено: ", "Total charged: ")
        + "<b>" + _money(bill["total_rub"]) + "</b>",
        tr("Подтверждённые оплаты: ", "Confirmed payments: ")
        + _money(bill["paid_rub"]),
    ])
    if Decimal(str(bill.get("credit_rub", 0))) > 0:
        lines.append(tr("Учтённые возвраты: ", "Refund credits: ")
                     + _money(bill["credit_rub"]))
    lines.append(tr("<b>Итого к оплате: ", "<b>Amount due: ")
                 + _money(bill["total_due_rub"]) + "</b>")
    return "\n".join(lines)
