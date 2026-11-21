"""Premium, bilingual onboarding and truthful owner trial presentation.

Pure presentation: no new callbacks, payment logic, fake trial clock or state.
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from telegram_membership_saas.localization import tr
from telegram_membership_saas.telegram_dates import native_telegram_time


def central_welcome_rich(*, language_selected: bool = False) -> str:
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
        + tr("150 ₽ или $5 за бота в месяц", "RUB 150 or USD 5 per bot/month")
        + "</td></tr>",
        "<tr><th>📈 " + tr("Продажи", "Sales") + "</th><td>"
        + tr("5% с подтверждённых покупок после пробного периода",
             "5% of verified purchases after your trial") + "</td></tr>",
        "</table>",
        *([] if language_selected else [
            "<h4>" + tr("🌐 Выберите язык интерфейса:", "🌐 Choose your interface language:")
            + "</h4>",
        ]),
        "__VIMI_RICH_ACTIONS__",
    ])


def central_welcome_classic(*, language_selected: bool = False) -> str:
    return "\n".join([
        "✨ <b>ViMi Pay</b>",
        "<b>" + tr(
            "Ваш Telegram-канал. Ваши подписчики. Ваш доход.",
            "Your channel. Your members. Your revenue.",
        ) + "</b>",
        "",
        "🎁 " + tr("72 часа бесплатно", "72 hours free"),
        "💳 " + tr(
            "Далее 150 ₽ или $5 / бот в месяц",
            "Then RUB 150 or USD 5 / bot per month",
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
        *([] if language_selected else [
            tr("🌐 Выберите язык интерфейса:", "🌐 Choose your interface language:"),
        ]),
    ])



def central_connection_rich(
    *, project_name: str, bot_username: str | None, runtime_label: str,
) -> str:
    """Compact connection control panel, not a second tenant admin menu."""
    label = (
        "@" + escape(bot_username, quote=True)
        if bot_username else tr("Бот ещё не подключён", "Bot not connected yet")
    )
    status = escape(runtime_label, quote=True)
    description = (
        tr(
            "🚀 <b>Открыть бота</b> — тарифы и продажи. "
            "⚙️ <b>Управление</b> — подключение и токен. "
            "🧾 <b>Счета</b> — оплата ViMi Pay.",
            "🚀 <b>Open bot</b> — plans and sales. "
            "⚙️ <b>Manage</b> — connection and token. "
            "🧾 <b>Invoices</b> — ViMi Pay service payments.",
        ) if bot_username else tr(
            "⚙️ Откройте управление подключением, чтобы добавить "
            "Telegram-бота. Счета доступны отдельно.",
            "⚙️ Open connection management to attach a Telegram bot. "
            "Invoices remain available separately.",
        )
    )
    return "\n".join([
        "<h3>✦ ViMi Pay · " + tr("Ваши подключения", "Your connections") + "</h3>",
        "<p><b>" + escape(project_name, quote=True) + "</b></p>",
        "<table bordered compact>",
        "<tr><th>" + tr("🤖 Бот", "🤖 Bot") + "</th><td><b>"
        + label + "</b></td></tr>",
        "<tr><th>" + tr("Состояние", "Status") + "</th><td>"
        + status + "</td></tr>",
        "</table>",
        "<p>" + description + "</p>",
    ])


def central_connection_classic(
    *, project_name: str, bot_username: str | None, runtime_label: str,
) -> str:
    """HTML fallback with the same truthful state and no duplicate actions."""
    label = (
        "@" + escape(bot_username, quote=True)
        if bot_username else tr("Бот ещё не подключён", "Bot not connected yet")
    )
    return (
        "✦ <b>ViMi Pay · "
        + tr("Ваши подключения", "Your connections") + "</b>\n"
        + "📁 <b>" + escape(project_name, quote=True) + "</b>\n"
        + "🤖 " + label + "\n"
        + escape(runtime_label, quote=True) + "\n\n"
        + (
            tr(
                "🚀 Откройте бота для тарифов и продаж. "
                "⚙️ Управление — для подключения. "
                "🧾 Счета — для оплаты ViMi Pay.",
                "🚀 Open the bot for plans and sales. "
                "⚙️ Manage — for the connection. "
                "🧾 Invoices — for ViMi Pay payments.",
            ) if bot_username else tr(
                "⚙️ Подключите Telegram-бота через управление.",
                "⚙️ Connect your Telegram bot through management.",
            )
        )
    )


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
