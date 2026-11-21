"""Owner-only central per-bot bill presentation (Rich + classic RU/EN).

The immutable issued bill remains the source of payable amounts. Current-month
sales are a separate read-only preview and must never become a synthetic debt.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape

from telegram_membership_saas.currency_display import display_currency
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.owner_setup.premium_welcome import (
    trial_status_classic,
)
from telegram_membership_saas.modules.platform_saas_bill_ui import (
    _bill_status,
    _billed_amount,
    unified_merchant_bill_classic,
    unified_merchant_bill_rich,
)
from telegram_membership_saas.rich_messages import rich_row_marker


def _safe_number(value: object) -> str:
    """Native amounts are quoted in their own currency, never added as RUB."""
    try:
        number = format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid per-bot sales amount") from exc
    return number.rstrip("0").rstrip(".") if "." in number else number


def _preview_details(
    *, sales: dict | None, currencies: dict | None, rich: bool,
) -> str:
    """The previews describe the current CALENDAR month, not the issued bill."""
    if sales is None or currencies is None:
        note = tr(
            "⚠️ Статистика продаж временно недоступна. Сумму долга это не меняет.",
            "⚠️ Sales statistics are temporarily unavailable. "
            "Your invoiced balance is unaffected.",
        )
        return "<p><i>" + note + "</i></p>" if rich else "<i>" + note + "</i>"
    rails = sales["rails"]
    grouped = {"subscription": 0, "file": 0}
    for item in rails:
        kind = str(item["kind"])
        if kind.startswith("subscription"):
            grouped["subscription"] += int(item["sold"])
        elif kind.startswith("file"):
            grouped["file"] += int(item["sold"])
    month = escape(str(sales.get("month") or "")[:7], quote=True)
    heading = tr("📊 ПРОДАЖИ ТЕКУЩЕГО МЕСЯЦА",
                 "📊 SALES THIS CALENDAR MONTH")
    sales_note = tr(
        "ℹ️ Показаны подтверждённые продажи. Это справочная статистика, "
        "не выставленная комиссия.",
        "ℹ️ Verified sales, for information only. "
        "This is not an issued commission charge.",
    )
    captions = (
        (tr("📢 Подписки", "📢 Subscriptions"), grouped["subscription"]),
        (tr("📁 Файлы", "📁 Files"), grouped["file"]),
    )
    turnover = [
        (str(row["currency"]), _safe_number(row["gross"]))
        for row in currencies["rows"]
    ]
    if rich:
        rows = [
            "<h4>" + heading + "</h4>",
            "<p><i>" + month + "</i></p>",
            "<table bordered compact>",
        ]
        for label, sold in captions:
            rows.append(
                "<tr><th>" + label + "</th><td>"
                + str(sold) + " "
                + tr("покупок", "sales") + "</td></tr>"
            )
        for currency, gross in turnover:
            rows.append(
                "<tr><th>" + tr("💰 Оборот", "💰 Turnover")
                + " · " + escape(display_currency(currency), quote=True)
                + "</th><td>" + escape(gross, quote=True) + "</td></tr>"
            )
        rows.append("</table>")
        if not turnover:
            rows.append("<p><i>" + tr(
                "Подтверждённых продаж пока нет.",
                "No verified turnover yet.",
            ) + "</i></p>")
        rows.append("<p><i>" + sales_note + "</i></p>")
        return "\n".join(rows)
    rows = ["<b>" + heading + "</b>" + (" · " + month if month else "")]
    rows.extend(
        label + ": " + str(sold) + " " + tr("покупок", "sales")
        for label, sold in captions
    )
    rows.extend(
        tr("💰 Оборот", "💰 Turnover") + " · "
        + escape(display_currency(currency), quote=True)
        + ": " + escape(gross, quote=True)
        for currency, gross in turnover
    )
    if not turnover:
        rows.append(tr("Подтверждённых продаж пока нет.",
                       "No verified turnover yet."))
    rows.append("<i>" + sales_note + "</i>")
    return "\n".join(rows)


def _trial_note(trial: dict | None, *, timezone: str, rich: bool) -> str:
    if trial is None:
        status = tr("⚠️ Статус пробного периода временно недоступен.",
                    "⚠️ Trial status is temporarily unavailable.")
    else:
        status = trial_status_classic(trial, timezone=timezone)
    if rich:
        return "<p>" + status.replace("\n", "<br>") + "</p>"
    return status


def bot_bill_rich(
    *, username: str, bill: dict, trial: dict | None, timezone: str,
    sales: dict | None, currencies: dict | None,
    future_currency: str,
) -> str:
    heading = "<h3>" + tr("🧾 СЧЁТ БОТА", "🧾 BOT BILL") + "</h3>"
    lines = [
        heading,
        "<p>🤖 <b>@" + escape(username.lstrip("@"), quote=True) + "</b></p>",
    ]
    if bill.get("invoice_id") is not None:
        # Use the authoritative existing issued-invoice renderer without
        # changing its amounts, payment marker positions or settlement logic.
        issued = unified_merchant_bill_rich(
            bill, future_profile_currency=future_currency,
        )
        lines.append(issued.partition("\n")[2])
        if bill.get("first_period"):
            lines.append("<p><i>" + tr(
                "🌱 Первый выставленный счёт включает только обслуживание. "
                "Продажи ниже не входят в этот счёт.",
                "🌱 The first issued bill is service-only. "
                "The current-month sales below are not part of this bill.",
            ) + "</i></p>")
            lines.append(_preview_details(
                sales=sales, currencies=currencies, rich=True,
            ))
        return "\n".join(lines)

    if trial is not None and trial.get("state") == "trial":
        lines.append("<h4>" + tr("🎁 ПРОБНЫЙ ПЕРИОД",
                                "🎁 FREE TRIAL") + "</h4>")
    else:
        lines.append("<h4>" + tr("📋 СТАТУС ОБСЛУЖИВАНИЯ",
                                "📋 SERVICE STATUS") + "</h4>")
    lines.append(_trial_note(trial, timezone=timezone, rich=True))
    if trial is not None and trial.get("state") == "trial":
        lines.append("<p><i>" + tr(
            "✨ Пока действует пробный период, счёт не выставлен "
            "и оплата не требуется.",
            "✨ While the free trial is active, no invoice has been issued "
            "and no payment is required.",
        ) + "</i></p>")
    else:
        lines.extend("<p>" + entry + "</p>" for entry in _bill_status(bill))
    rate = (
        "USD · $5/" + tr("мес.", "month")
        if future_currency == "USD"
        else "RUB · 150 ₽/" + tr("мес.", "month")
    )
    lines.extend([
        "<h4>" + tr("🤖 ОБСЛУЖИВАНИЕ БОТА",
                     "🤖 BOT SERVICE") + "</h4>",
        "<table bordered compact><tr><th>"
        + tr("💳 Будущая абонплата", "💳 Future service fee")
        + "</th><td><b>" + rate + "</b></td></tr></table>",
    ])
    if trial is not None and trial.get("state") == "exempt":
        lines.append("<p><i>" + tr(
            "👑 Для собственных ботов платформы обслуживание бесплатно.",
            "👑 Platform-owned bots are exempt from service fees.",
        ) + "</i></p>")
    lines.append(_preview_details(
        sales=sales, currencies=currencies, rich=True,
    ))
    lines.extend([
        "<h4>" + tr("💰 ФИНАНСОВЫЙ ИТОГ",
                     "💰 FINANCIAL SUMMARY") + "</h4>",
        "<table bordered compact><tr><th>"
        + tr("Начислено по выставленным счетам",
             "Charged on issued invoices")
        + "</th><td>0 ₽</td></tr>",
        "<tr><th>" + tr("✅ Сейчас к оплате", "✅ Amount due now")
        + "</th><td><b>0 ₽</b></td></tr></table>",
        "<p><i>" + tr(
            "Реальный счёт ещё не выставлен. Продажи не считаются долгом.",
            "No real invoice has been issued yet. Sales are not a debt.",
        ) + "</i></p>",
        rich_row_marker(0),
    ])
    return "\n".join(lines)


def bot_bill_classic(
    *, username: str, bill: dict, trial: dict | None, timezone: str,
    sales: dict | None, currencies: dict | None,
    future_currency: str,
) -> str:
    lines = [
        tr("🧾 <b>СЧЁТ БОТА</b>", "🧾 <b>BOT BILL</b>"),
        "🤖 <b>@" + escape(username.lstrip("@"), quote=True) + "</b>",
        "",
    ]
    if bill.get("invoice_id") is not None:
        issued = unified_merchant_bill_classic(
            bill, future_profile_currency=future_currency,
        )
        lines.append(issued.partition("\n")[2])
        if bill.get("first_period"):
            lines.extend(["", "<i>" + tr(
                "🌱 Первый выставленный счёт содержит только обслуживание. "
                "Продажи ниже не входят в текущий счёт.",
                "🌱 The first issued bill is service-only. "
                "The sales below are not in this invoice.",
            ) + "</i>", "", _preview_details(
                sales=sales, currencies=currencies, rich=False,
            )])
        return "\n".join(lines)
    lines.append(_trial_note(trial, timezone=timezone, rich=False))
    if trial is not None and trial.get("state") == "trial":
        lines.extend(["", "<i>" + tr(
            "✨ Пробный период активен: платить пока не нужно.",
            "✨ Free trial is active: no payment is due yet.",
        ) + "</i>"])
    else:
        lines.extend(_bill_status(bill))
    rate = (
        "USD · $5/" + tr("мес.", "month")
        if future_currency == "USD"
        else "RUB · 150 ₽/" + tr("мес.", "month")
    )
    lines.extend([
        "", tr("🤖 <b>ОБСЛУЖИВАНИЕ БОТА</b>",
               "🤖 <b>BOT SERVICE</b>"),
        tr("💳 Будущая абонплата: ", "💳 Future service fee: ")
        + "<b>" + rate + "</b>",
        "", _preview_details(sales=sales, currencies=currencies, rich=False),
        "", tr("💰 <b>ФИНАНСОВЫЙ ИТОГ</b>",
               "💰 <b>FINANCIAL SUMMARY</b>"),
        tr("Начислено по выставленным счетам: 0 ₽",
           "Charged on issued invoices: 0 ₽"),
        tr("✅ <b>Сейчас к оплате: 0 ₽</b>",
           "✅ <b>Amount due now: 0 ₽</b>"),
        "<i>" + tr(
            "Счёт ещё не выставлен. Продажи не считаются долгом.",
            "No invoice issued yet. Sales are not a debt.",
        ) + "</i>",
    ])
    return "\n".join(lines)
