"""Premium, bilingual onboarding and truthful owner trial presentation.

Pure presentation: no new callbacks, payment logic, fake trial clock or state.
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from telegram_membership_saas.localization import tr
from telegram_membership_saas.telegram_dates import native_telegram_time


def central_welcome_rich() -> str:
    """One compact Telegram Rich card with the existing connection actions."""
    return "\n".join([
        "<h3>✨ ViMi Pay</h3>",
        "<p><b>" + tr(
            "Ваш Telegram-канал. Ваши подписчики. Ваш доход.",
            "Your channel. Your members. Your revenue.",
        ) + "</b></p>",
        "<p>" + tr(
            "Подключите бота, настройте тарифы и начните принимать оплату "
            "за подписки и файлы в единой панели.",
            "Connect a bot, create plans and collect subscription and "
            "file payments from one control panel.",
        ) + "</p>",
        "<table bordered compact>",
        "<tr><th>🎁 " + tr("Старт", "Start") + "</th><td>"
        + tr("72 часа бесплатно после первого подключения",
             "72 hours free after your first connection") + "</td></tr>",
        "<tr><th>💳 " + tr("Обслуживание", "Service") + "</th><td>"
        + tr("150 ₽ или $3 за бота в месяц", "RUB 150 or USD 3 per bot/month")
        + "</td></tr>",
        "<tr><th>📈 " + tr("Продажи", "Sales") + "</th><td>"
        + tr("5% с подтверждённых покупок после пробного периода",
             "5% of verified purchases after your trial") + "</td></tr>",
        "</table>",
        "<p>👇 " + tr(
            "Создайте бота или подключите существующего через BotFather.",
            "Create a bot or connect an existing one via BotFather.",
        ) + "</p>",
    ])


def central_welcome_classic() -> str:
    return "\n".join([
        "✨ <b>ViMi Pay</b>",
        "<b>" + tr(
            "Ваш Telegram-канал. Ваши подписчики. Ваш доход.",
            "Your channel. Your members. Your revenue.",
        ) + "</b>",
        "",
        "🎁 " + tr("72 часа бесплатно", "72 hours free"),
        "💳 " + tr(
            "Далее 150 ₽ или $3 / бот в месяц",
            "Then RUB 150 or USD 3 / bot per month",
        ),
        "📈 " + tr(
            "5% с подтверждённых продаж после пробного периода",
            "5% of verified sales after the trial",
        ),
        "",
        tr(
            "Подключите бота, настройте тарифы, платежи и продажу файлов.",
            "Connect a bot, set up plans, payments and file sales.",
        ),
        "👇 " + tr(
            "Выберите способ подключения ниже.",
            "Choose a connection method below.",
        ),
    ])


def _trial_ends_html(value: object, timezone: str) -> str:
    """DB supplies the exact claim UTC timestamp; never infer it locally."""
    if not isinstance(value, str):
        return ""
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            return ""
        return native_telegram_time(date, timezone)
    except (ValueError, OverflowError):
        return escape(value)


def trial_status_classic(data: dict, *, timezone: str) -> str:
    """Short, accurate status for the existing connected bot and bill card."""
    state = str(data.get("state", "pending"))
    if state == "exempt":
        return tr(
            "👑 Собственные боты ViMi Pay: без абонплаты и комиссии.",
            "👑 Your ViMi Pay bots: no service fee or commission.",
        )
    if state == "not_connected":
        return tr("🤖 Подключите первого бота.", "🤖 Connect your first bot.")
    if state == "disabled":
        return tr(
            "⚪ Монетизация платформы пока выключена.",
            "⚪ Platform billing is currently disabled.",
        )
    if state == "scheduled":
        date = _trial_ends_html(data.get("billing_effective_from"), timezone)
        return tr(
            "🕒 Начало коммерческого учёта: ",
            "🕒 Commercial billing starts: ",
        ) + (date or tr("скоро", "soon"))
    if state == "pending":
        return tr(
            "⏳ Пробный период пока не зарегистрирован. Проверьте статус позже.",
            "⏳ Trial registration is pending. Check the status later.",
        )
    if state == "ended":
        return tr(
            "⌛ Пробный период завершён. Проверьте 🧾 Счёт.",
            "⌛ Trial ended. Check 🧾 Bill.",
        )
    if state != "trial":
        return tr("⚪ Статус периода неизвестен.", "⚪ Trial status unavailable.")

    remaining = max(0, int(data.get("remaining_seconds") or 0))
    days, hours = divmod((remaining + 3599) // 3600, 24)
    duration = (
        tr(f"{days} дн. {hours} ч.", f"{days}d {hours}h")
        if days else tr(f"{hours} ч.", f"{hours}h")
    )
    ends = _trial_ends_html(data.get("trial_ends_at"), timezone)
    return (
        tr("🎁 Пробный период: осталось ", "🎁 Free trial: ")
        + escape(duration)
        + (tr("\n🗓️ До: ", "\n🗓️ Until: ") + ends if ends else "")
    )


def trial_status_rich(data: dict, *, timezone: str) -> str:
    status = trial_status_classic(data, timezone=timezone)
    return (
        "<h3>" + tr("⏳ Статус обслуживания", "⏳ Service status") + "</h3>\n"
        "<p>" + status.replace("\n", "<br>") + "</p>"
    )
