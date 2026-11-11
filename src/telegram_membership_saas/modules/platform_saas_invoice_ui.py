"""Shared RU/EN Rich cards and classic fallback for issued SaaS invoices.

No synthetic invoices or bank details. Credit notes remain unapplied until a
separately reviewed financial allocation policy exists.
"""
from __future__ import annotations

from html import escape
from decimal import Decimal

from telegram_membership_saas.localization import tr


def _safe(value: object) -> str:
    return escape(str(value), quote=True)


def _money(value: object) -> str:
    return _safe(format(Decimal(str(value)), ".2f"))


def _type_label(value: str) -> str:
    return (
        tr("Обслуживание бота", "Bot service")
        if value == "prepayment" else tr("Комиссия с продаж", "Sales commission")
    )


def _state_label(value: str) -> str:
    return tr("Оплачено", "Paid") if value == "paid" else tr(
        "Ожидает оплаты", "Pending payment",
    )


def _audit_rows(invoice: dict, snapshot: dict, credits: dict) -> list[tuple[str, str]]:
    amount_rub = invoice.get("amount_rub", invoice.get("total_rub", "0"))
    rows = [
        (tr("Тип", "Type"), _type_label(str(invoice["kind"]))),
        (tr("Статус", "Status"), _state_label(str(invoice["status"]))),
        (tr("Базовая сумма", "RUB base"), _money(amount_rub) + " ₽"),
        (tr("Оплатить до", "Due"), _safe(str(invoice["due_at"])[:10])),
    ]
    if snapshot.get("locked"):
        currency = _safe(snapshot["currency"])
        rows.extend([
            (tr("Валюта зафиксирована", "Locked currency"), currency),
            (tr("Сумма к оплате", "Locked amount"),
             _money(snapshot["amount"]) + " " + currency),
            (tr("Курс", "FX rate"),
             "1 " + currency + " = " + _safe(snapshot["rub_per_unit"]) + " ₽"),
            (tr("Дата фиксации", "Quote date"),
             _safe(snapshot["quote_day"])),
            (tr("Источник курса", "FX source"),
             _safe(snapshot.get("source") or "—") + " · "
             + _safe(snapshot.get("source_day") or snapshot["quote_day"])),
        ])
    else:
        rows.append((
            tr("Валюта платежа", "Payment currency"),
            tr("Не выбрана", "Not selected"),
        ))
    rows.append((
        tr("Возвраты по счёту", "Invoice refund credits"),
        _money(credits.get("total_credited_rub", 0)) + " ₽",
    ))
    return rows


def issued_invoice_rich(invoice: dict, snapshot: dict, credits: dict) -> str:
    """Native Rich tables, with all external contents HTML-escaped."""
    lines = [
        "<h3>" + tr("🧾 ViMi Pay · Выставленный счёт",
                     "🧾 ViMi Pay · Issued invoice") + "</h3>",
        "<p>№ <code>" + _safe(invoice["id"]) + "</code></p>",
        "<table bordered compact>",
        "<tr><th>" + tr("Показатель", "Field") + "</th><th>"
        + tr("Значение", "Value") + "</th></tr>",
    ]
    for label, value in _audit_rows(invoice, snapshot, credits):
        lines.append("<tr><td>" + label + "</td><td>" + value + "</td></tr>")
    lines.append("</table>")
    if credits.get("entries"):
        lines.extend([
            "<h4>" + tr("↩️ История корректировок",
                         "↩️ Credit history") + "</h4>",
            "<table bordered compact>",
            "<tr><th>" + tr("Основание", "Reason") + "</th><th>"
            + tr("Сумма, ₽", "RUB credit") + "</th></tr>",
        ])
        for credit in credits["entries"]:
            lines.append(
                "<tr><td>" + _safe(credit["reason"]) + " · "
                + _safe(credit["reference"]) + "</td><td>"
                + _money(credit["credit_rub"]) + "</td></tr>"
            )
        lines.append("</table>")
    lines.append("<p><i>" + tr(
        "Корректировки пока не вычитаются автоматически из выставленных "
        "счетов и не снимают ограничения. Исходные суммы сохраняются.",
        "Credit notes are not automatically deducted from issued bills "
        "or used to release holds. Original amounts remain unchanged.",
    ) + "</i></p>")
    if snapshot.get("locked") and snapshot["currency"] != "RUB":
        lines.append("<p>⚠️ " + tr(
            "Реквизиты для выбранной валюты пока не подтверждены. "
            "Не переводите средства по рублёвым реквизитам.",
            "Payment instructions for this currency are not yet verified. "
            "Do not use RUB bank details for foreign transfers.",
        ) + "</p>")
    return "\n".join(lines)


def issued_invoice_classic(invoice: dict, snapshot: dict, credits: dict) -> str:
    lines = [
        tr("<b>🧾 ViMi Pay · Выставленный счёт</b>",
           "<b>🧾 ViMi Pay · Issued invoice</b>"),
        "№ <code>" + _safe(invoice["id"]) + "</code>",
    ]
    for label, value in _audit_rows(invoice, snapshot, credits):
        lines.append(label + ": " + value)
    for credit in credits.get("entries", []):
        lines.append(
            "↩️ " + _safe(credit["reason"]) + " · "
            + _safe(credit["reference"]) + " · "
            + _money(credit["credit_rub"]) + " ₽"
        )
    lines.append("<i>" + tr(
        "Возвраты сохранены отдельно; они ещё не уменьшают сумму счёта.",
        "Credits are audited separately and do not yet reduce this invoice.",
    ) + "</i>")
    if snapshot.get("locked") and snapshot["currency"] != "RUB":
        lines.append("⚠️ " + tr(
            "Не используйте рублёвые реквизиты для валютного платежа.",
            "Do not use RUB bank instructions for this foreign payment.",
        ))
    return "\n".join(lines)


def issued_quote_rich(quote: dict) -> str:
    lines = [
        "<h3>" + tr("💱 Подтвердить валюту", "💱 Confirm currency") + "</h3>",
        "<p>№ <code>" + _safe(quote["invoice_id"]) + "</code></p>",
        "<table bordered compact>",
        "<tr><td>" + tr("База", "RUB base") + "</td><td>"
        + _money(quote["total_rub"]) + " ₽</td></tr>",
        "<tr><td>" + tr("Валюта", "Currency") + "</td><td>"
        + _safe(quote["currency"]) + "</td></tr>",
        "<tr><td>" + tr("Дата курса", "Quote date") + "</td><td>"
        + _safe(quote["quote_day"]) + "</td></tr>",
    ]
    if quote["quote_ready"]:
        lines.extend([
            "<tr><td>" + tr("Официальный курс", "Official rate")
            + "</td><td>1 " + _safe(quote["currency"]) + " = "
            + _safe(quote["rub_per_unit"]) + " ₽</td></tr>",
            "<tr><td>" + tr("Дата источника", "Source date")
            + "</td><td>" + _safe(quote.get("source_day") or "RUB")
            + "</td></tr>",
            "<tr><td>" + tr("Сумма", "Amount") + "</td><td><b>"
            + _money(quote["amount"]) + " "
            + _safe(quote["currency"]) + "</b></td></tr>",
        ])
    lines.append("</table>")
    if quote["quote_ready"]:
        lines.append("<p><b>⚠️ " + tr(
            "После подтверждения валюту и курс изменить нельзя.",
            "Once confirmed, the currency and rate cannot be changed.",
        ) + "</b></p>")
    else:
        lines.append("<p>⚠️ " + tr(
            "Нет подтверждённого курса на сегодня. Фиксация недоступна.",
            "No verified quote-day FX. Currency locking is unavailable.",
        ) + "</p>")
    lines.append("<p><i>" + tr(
        "Это предварительный расчёт. Само подтверждение валюты "
        "не подтверждает поступление денег.",
        "This is a preview. Locking a currency never confirms bank receipt.",
    ) + "</i></p>")
    return "\n".join(lines)


def issued_quote_classic(quote: dict) -> str:
    return "\n".join([
        tr("<b>💱 Подтвердить валюту</b>",
           "<b>💱 Confirm currency</b>"),
        "№ <code>" + _safe(quote["invoice_id"]) + "</code>",
        tr("База: ", "Base: ") + _money(quote["total_rub"]) + " ₽",
        tr("Валюта: ", "Currency: ") + _safe(quote["currency"]),
        tr("Дата: ", "Quote date: ") + _safe(quote["quote_day"]),
        (tr("Курс: ", "Rate: ") + _safe(quote["rub_per_unit"])
         + " ₽, " + tr("сумма: ", "amount: ")
         + _money(quote["amount"]) + " " + _safe(quote["currency"]))
        if quote["quote_ready"] else tr(
            "⚠️ Нет курса. Подтверждение недоступно.",
            "⚠️ Rate unavailable. Cannot lock currency.",
        ),
        "<i>" + tr(
            "После подтверждения валюта и курс неизменяемы. "
            "Это не подтверждение оплаты.",
            "Currency and rate become immutable. This does not confirm payment.",
        ) + "</i>",
    ])
