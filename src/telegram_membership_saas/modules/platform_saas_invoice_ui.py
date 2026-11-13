"""Shared RU/EN Rich cards and classic fallback for issued SaaS invoices.

No synthetic invoices or bank details. Verified Stars/Crypto credits reduce
real balances; historical manual notes remain separate.
"""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from html import escape

from telegram_membership_saas.localization import tr


def _fingerprint(parts: tuple[object, ...]) -> str:
    """Short stable token for Telegram callbacks, never an authorization grant."""
    return sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:12]


def quote_fingerprint(quote: dict) -> str:
    """A stale rate/date/amount changes the token and blocks a final FX lock."""
    return _fingerprint((
        quote["invoice_id"], quote["currency"], quote["quote_day"],
        quote.get("source_day"), quote.get("rub_per_unit"),
        quote.get("amount"), quote["total_rub"],
    ))


def receipt_fingerprint(invoice_id: object, invoice: dict, snapshot: dict) -> str:
    """Binds operator's confirmation to exactly what the card displayed."""
    return _fingerprint((
        invoice_id, invoice["status"],
        invoice.get("amount_rub", invoice.get("total_rub")),
        snapshot.get("locked", False), snapshot.get("currency"),
        snapshot.get("quote_day"), snapshot.get("rub_per_unit"),
        snapshot.get("amount"),
    ))


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


def _audit_rows(
    invoice: dict, snapshot: dict, credits: dict, balance: dict | None = None,
) -> list[tuple[str, str]]:
    amount_rub = invoice.get("amount_rub", invoice.get("total_rub", "0"))
    rows = [
        (tr("Тип", "Type"), _type_label(str(invoice["kind"]))),
        (tr("Статус", "Status"), _state_label(
            "paid" if balance and balance.get("paid") else str(invoice["status"])
        )),
        (tr("Базовая сумма", "RUB base"), _money(amount_rub) + " ₽"),
        (tr("Оплатить до", "Due"), _safe(str(invoice["due_at"])[:10])),
    ]
    if snapshot.get("locked"):
        currency = _safe(snapshot["currency"])
        rows.extend([
            (tr("Валюта зафиксирована", "Locked currency"), currency),
            (tr("Историческая сумма фиксации", "Historical locked amount"),
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
    if balance is not None:
        rows.extend([
            (tr("Зачтённые возвраты Stars/Crypto",
                "Applied Stars/Crypto refunds"),
             _money(balance.get("verified_refund_credit_rub", 0)) + " ₽"),
            (tr("Остаток к оплате", "Remaining due"),
             "<b>" + _money(balance.get("remaining_rub", 0)) + " ₽</b>"),
        ])
    rows.append((
        tr("Отдельные корректировки (не зачтены)",
           "Separate manual adjustments (unapplied)"),
        _money(credits.get("total_credited_rub", 0)) + " ₽",
    ))
    return rows


def issued_invoice_rich(
    invoice: dict, snapshot: dict, credits: dict,
    balance: dict | None = None,
) -> str:
    """Native Rich tables, with all external contents HTML-escaped."""
    lines = [
        "<h3>" + tr("🧾 ViMi Pay · Выставленный счёт",
                     "🧾 ViMi Pay · Issued invoice") + "</h3>",
        "<p>№ <code>" + _safe(invoice["id"]) + "</code></p>",
        "<table bordered compact>",
        "<tr><th>" + tr("Показатель", "Field") + "</th><th>"
        + tr("Значение", "Value") + "</th></tr>",
    ]
    for label, value in _audit_rows(invoice, snapshot, credits, balance):
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
        "Проверенные возвраты Stars/Crypto уже учтены в остатке. "
        "Отдельные ручные корректировки сохраняются как история.",
        "Verified Stars/Crypto refunds reduce the balance. "
        "Separate manual adjustments remain historical notes.",
    ) + "</i></p>")
    if snapshot.get("locked") and snapshot["currency"] != "RUB":
        lines.append("<p>⚠️ " + tr(
            "Реквизиты для выбранной валюты пока не подтверждены. "
            "Не переводите средства по рублёвым реквизитам.",
            "Payment instructions for this currency are not yet verified. "
            "Do not use RUB bank details for foreign transfers.",
        ) + "</p>")
    return "\n".join(lines)


def issued_invoice_classic(
    invoice: dict, snapshot: dict, credits: dict,
    balance: dict | None = None,
) -> str:
    lines = [
        tr("<b>🧾 ViMi Pay · Выставленный счёт</b>",
           "<b>🧾 ViMi Pay · Issued invoice</b>"),
        "№ <code>" + _safe(invoice["id"]) + "</code>",
    ]
    for label, value in _audit_rows(invoice, snapshot, credits, balance):
        lines.append(label + ": " + value)
    for credit in credits.get("entries", []):
        lines.append(
            "↩️ " + _safe(credit["reason"]) + " · "
            + _safe(credit["reference"]) + " · "
            + _money(credit["credit_rub"]) + " ₽"
        )
    lines.append("<i>" + tr(
        "Возвраты Stars/Crypto учитываются в остатке, ручные корректировки отдельно.",
        "Stars/Crypto refunds reduce the balance; manual adjustments remain separate.",
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


def unified_monthly_rich(report: dict) -> str:
    """Compact verified-source commission snapshot, not an issued invoice."""
    if not isinstance(report.get("rails"), list):
        raise ValueError("Missing unified financial sources")
    labels = {
        "subscription": tr("Подписки · переводы", "Subscriptions · manual"),
        "file_manual": tr("Файлы · переводы", "Files · manual"),
        "file_stars": tr("Файлы · Stars", "Files · Stars"),
        "subscription_stars": tr("Подписки · Stars", "Subscriptions · Stars"),
        "file_crypto": tr("Файлы · Crypto Pay", "Files · Crypto Pay"),
        "subscription_crypto": tr("Подписки · Crypto Pay", "Subscriptions · Crypto Pay"),
    }
    html = [
        "<h3>" + tr("📊 Начисления ViMi Pay", "📊 ViMi Pay accruals") + "</h3>",
        "<p>" + _safe(str(report["month"])[:7]) + " · "
        + (tr("Предпросмотр, начисления отключены",
              "Preview, commercial billing is disabled")
           if not report["billing_enabled"] else
           tr("Только предварительные начисления",
              "Provisional charges only")) + "</p>",
        "<table bordered compact>",
        "<tr><th>" + tr("Продажи", "Sales") + "</th><th>"
        + tr("Кол-во", "Count") + "</th><th>"
        + tr("Комиссия ₽", "Commission ₽") + "</th></tr>",
    ]
    for item in report["rails"]:
        html.append(
            "<tr><td>" + labels.get(item["kind"], _safe(item["kind"]))
            + "</td><td>" + str(int(item["sold"]))
            + "</td><td>" + _money(item["commission_rub"])
            + "</td></tr>"
        )
    html.extend([
        "</table>",
        "<p>" + tr("5% предварительно: ", "5% provisional: ")
        + "<b>" + _money(report["commission_rub"]) + " ₽</b></p>",
    ])
    if report["unrated_billable"] or report["uncaptured_crypto"]:
        html.append("<p>" + tr(
            "⚠️ Требуют подтверждения: ", "⚠️ Needs verification: "
        ) + str(int(report["unrated_billable"])) + " FX · "
        + str(int(report["uncaptured_crypto"])) + " Crypto Pay</p>")
    star_credit = Decimal(str(report["star_refund_credit_usd"]))
    crypto_credit = Decimal(str(report["crypto_refund_credit_usd"]))
    if star_credit or crypto_credit:
        html.append(
            "<p>" + tr("↩️ Возвраты: Stars $", "↩️ Refunds: Stars $")
            + _money(star_credit)
            + tr(" · Crypto Pay $", " · Crypto Pay $")
            + _money(crypto_credit)
            + tr(" (зачёт см. в выставленных счетах)",
               " (see issued invoices for applied credits)") + "</p>"
        )
    return "\n".join(html)


def unified_monthly_classic(report: dict) -> str:
    """HTML fallback shares exactly the data in the Rich card."""
    if not isinstance(report.get("rails"), list):
        raise ValueError("Missing unified financial sources")
    labels = {
        "subscription": tr("Подписки · переводы", "Subscriptions · manual"),
        "file_manual": tr("Файлы · переводы", "Files · manual"),
        "file_stars": tr("Файлы · Stars", "Files · Stars"),
        "subscription_stars": tr("Подписки · Stars", "Subscriptions · Stars"),
        "file_crypto": tr("Файлы · Crypto Pay", "Files · Crypto Pay"),
        "subscription_crypto": tr("Подписки · Crypto Pay", "Subscriptions · Crypto Pay"),
    }
    rows = [
        tr("📊 <b>Начисления ViMi Pay</b>",
           "📊 <b>ViMi Pay accruals</b>"),
        _safe(str(report["month"])[:7]),
    ]
    for item in report["rails"]:
        rows.append(
            "• " + labels.get(item["kind"], _safe(item["kind"]))
            + " · " + str(int(item["sold"])) + " · "
            + _money(item["commission_rub"]) + " ₽"
        )
    rows.extend([
        tr("5% предварительно: ", "5% provisional: ")
        + "<b>" + _money(report["commission_rub"]) + " ₽</b>",
        tr("ℹ️ Это не выставленный счёт.",
           "ℹ️ This is not an issued invoice."),
    ])
    if report["unrated_billable"] or report["uncaptured_crypto"]:
        rows.append(
            tr("⚠️ Неоценённых источников: ", "⚠️ Unvalued sources: ")
            + str(int(report["unrated_billable"])
                  + int(report["uncaptured_crypto"]))
        )
    return "\n".join(rows)
