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


def _invoice_due_label(value: object) -> str:
    """A readable date for the owner, never a raw timestamp."""
    value = str(value)[:10]
    try:
        year, month, day = (int(part) for part in value.split("-"))
    except (ValueError, TypeError):
        return _safe(value)
    return tr(f"{day:02d}.{month:02d}.{year}", value)


def _invoice_summary_rows(
    invoice: dict, snapshot: dict, credits: dict,
    balance: dict | None,
) -> list[tuple[str, str]]:
    amount_rub = Decimal(str(invoice.get(
        "amount_rub", invoice.get("total_rub", 0),
    )))
    locked = snapshot.get("locked", False)
    if locked:
        amount = _money(snapshot["amount"]) + " " + _safe(snapshot["currency"])
    else:
        amount = _money(amount_rub) + " ₽"
    paid = invoice["status"] == "paid" or bool(
        balance and balance.get("paid"),
    )
    rows = [
        (tr("За что", "For"), _type_label(str(invoice["kind"]))),
        (tr("Сумма", "Amount"), "<b>" + amount + "</b>"),
        (tr("Оплатить до", "Pay by"), _invoice_due_label(invoice["due_at"])),
        (tr("Статус", "Status"), _state_label(
            "paid" if paid else str(invoice["status"]),
        )),
    ]
    if locked and str(snapshot.get("currency")) != "RUB":
        rows.append((
            tr("Рублёвый эквивалент", "RUB equivalent"),
            _money(amount_rub) + " ₽",
        ))
    if balance is not None and not paid:
        rows.append((
            tr("Осталось оплатить", "Amount remaining"),
            "<b>" + _money(balance.get("remaining_rub", 0)) + " ₽</b>",
        ))
        credited = Decimal(str(balance.get(
            "verified_refund_credit_rub", 0,
        )))
        if credited:
            rows.append((
                tr("Учтённые возвраты", "Refund credits applied"),
                _money(credited) + " ₽",
            ))
    manual = Decimal(str(credits.get("total_credited_rub", 0)))
    if manual:
        rows.append((
            tr("Отдельные корректировки (не оплачивают счёт)",
               "Separate adjustments (do not pay this invoice)"),
            _money(manual) + " ₽",
        ))
    return rows


def issued_invoice_rich(
    invoice: dict, snapshot: dict, credits: dict,
    balance: dict | None = None,
) -> str:
    """Customer-facing concise bill; bank audit stays in finance services."""
    rows = _invoice_summary_rows(invoice, snapshot, credits, balance)
    return "\n".join([
        "<h3>" + tr("🧾 Счёт ViMi Pay", "🧾 ViMi Pay invoice") + "</h3>",
        *("<p>" + label + ": " + value + "</p>" for label, value in rows),
    ])


def issued_invoice_classic(
    invoice: dict, snapshot: dict, credits: dict,
    balance: dict | None = None,
) -> str:
    rows = _invoice_summary_rows(invoice, snapshot, credits, balance)
    return "\n".join([
        tr("🧾 <b>Счёт ViMi Pay</b>", "🧾 <b>ViMi Pay invoice</b>"),
        "",
        *(label + ": " + value for label, value in rows),
    ])


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
