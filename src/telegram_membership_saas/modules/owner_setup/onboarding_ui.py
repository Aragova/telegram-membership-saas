"""Compact bilingual setup wizard for newly registered bots.

The actual operations continue to use the existing channel, payment, plans,
and per-bot currency handlers. No project names, phantom buttons or duplicate
payment paths are introduced.
"""
from __future__ import annotations

from html import escape

from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER

_STEP = {
    "resources": 1,
    "methods": 2,
    "plans": 3,
    "saasprofile": 4,
}


def owner_setup_step_rich(
    *, bot_name: str, timezone: str, action: str,
) -> str:
    step = _STEP.get(action, 1)
    items = {
        "resources": (
            tr("📢 Канал или группа", "📢 Channel or group"),
            tr("Добавьте своего бота в администраторы закрытого канала "
               "или группы. Предоставьте права на создание приглашений "
               "и управление участниками. Затем отправьте канал или группу "
               "боту для автоматической проверки.",
               "Make this bot an administrator of a private channel or "
               "group. Grant rights for invite links and membership "
               "management. Then share the channel or group for a live check."),
        ),
        "methods": (
            tr("💳 Оплата покупателей", "💳 Buyer payments"),
            tr("Выберите хотя бы один рабочий способ оплаты: ⭐ Telegram "
               "Stars, Crypto Pay или полуавтоматический перевод. Укажите "
               "реальные реквизиты и инструкции. Это платежи покупателей, "
               "не счета обслуживания ViMi Pay.",
               "Configure at least one working method: ⭐ Telegram Stars, "
               "Crypto Pay or manual transfer. Provide real payment "
               "instructions. These are buyer payments, not ViMi Pay bills."),
        ),
        "plans": (
            tr("📦 Первый тариф", "📦 First plan"),
            tr("Укажите название, срок, цену для покупателя и ресурс, "
               "к которому откроется доступ. Сохраняйте тариф; позже "
               "можно добавить другие.",
               "Choose a name, duration, buyer price and the resource "
               "to unlock. Save this plan; you can add more later."),
        ),
        "saasprofile": (
            tr("💱 Валюта обслуживания", "💱 Service currency"),
            tr("Последний шаг: выберите 🇷🇺 RUB (150 ₽/месяц) или "
               "🇺🇸 USD ($5/месяц). Первый счёт только за обслуживание, "
               "со следующего периода добавляется 5% подтверждённых "
               "продаж. Выбор относится только к этому боту.",
               "Final step: choose 🇷🇺 RUB (150 RUB/month) or 🇺🇸 USD "
               "(5 USD/month). The first bill is service-only; later bills "
               "also include 5% of confirmed sales. This is per bot."),
        ),
    }
    title, description = items.get(action, items["resources"])
    return "\n".join([
        "<h3>" + tr("✨ Настройка ViMi Pay", "✨ ViMi Pay setup") + "</h3>",
        "<p><b>" + escape(bot_name) + "</b> 🌍 " + escape(timezone) + "</p>",
        "<table bordered compact><tr><th>" + tr(
            "Этап", "Step",
        ) + "</th><td>" + str(step) + "/4</td></tr></table>",
        "<h4>" + title + "</h4>",
        "<p>" + description + "</p>",
        "<p>" + tr(
            "Уже сохранённые каналы, тарифы и платежи останутся на месте. "
            "После завершения откроется полная панель администратора.",
            "Saved channels, plans and payments are preserved. The full "
            "dashboard opens once setup is complete.",
        ) + "</p>",
        RICH_ACTIONS_MARKER,
    ])


def owner_setup_step_classic(
    *, bot_name: str, timezone: str, action: str,
) -> str:
    step = _STEP.get(action, 1)
    return (
        tr("✨ <b>Настройка ViMi Pay</b>", "✨ <b>ViMi Pay setup</b>")
        + "\n🤖 <b>" + escape(bot_name) + "</b> 🌍 "
        + escape(timezone) + "\n"
        + tr("Этап ", "Step ") + str(step) + "/4\n\n"
        + tr("Следуйте кнопке ниже для продолжения. Все настройки "
             "сохраняются.", "Use the button below to continue. Your "
             "progress is saved.")
    )
