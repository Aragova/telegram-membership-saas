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


def _channel_setup_copy(bot_name: str) -> tuple[str, str]:
    """Detailed RU/EN onboarding copy using the exact connected bot handle.

    Channel verification checks Telegram's can_invite_users AND
    can_restrict_members; labels can differ between Telegram clients.
    """
    handle = "<b>@" + escape(bot_name.lstrip("@")) + "</b>"
    ru = "\n".join([
        "<b>📢 ПОДКЛЮЧЕНИЕ КАНАЛА ИЛИ ГРУППЫ</b>",
        "Для продолжения настройки добавьте <b>именно ЭТОГО бота</b> "
        + handle + " в администраторы <b>закрытого канала или группы</b>. "
        "Это позволит выдавать покупателям доступ и отключать его "
        "после завершения подписки.",
        "<b>📋 ПОШАГОВАЯ ИНСТРУКЦИЯ</b>",
        "<b>1️⃣ Добавьте бота в администраторы.</b>\n"
        "Откройте свой канал или группу → нажмите на название → "
        "«Управление» / «Изменить» → «Администраторы» → "
        "«Добавить администратора». Найдите " + handle
        + " и выберите именно его. "
        "<i>Обычного добавления бота в участники недостаточно.</i>",
        "<b>2️⃣ Включите ДВА обязательных права:</b>\n"
        "✅ <b>Пригласительные ссылки / добавление пользователей</b> "
        "— для выдачи доступа после оплаты.\n"
        "✅ <b>Блокировка / удаление пользователей</b> "
        "— для завершения доступа после окончания подписки.\n"
        "<i>Названия переключателей могут немного отличаться в Telegram.</i>",
        "<b>3️⃣ Подтвердите канал или группу в ViMi Pay.</b>\n"
        "Сохраните права администратора и вернитесь <b>в этот чат</b>. "
        "Нажмите кнопку ниже, выберите канал или группу и следуйте "
        "инструкции Telegram. ViMi Pay проверит ваши права и права бота.",
        "<b>🛡 ВАШИ НАСТРОЙКИ СОХРАНЯТСЯ</b>\n"
        "Уже сохранённые каналы, тарифы, способы оплаты и другие "
        "настройки останутся на месте.",
    ])
    en = "\n".join([
        "<b>📢 CONNECT A CHANNEL OR GROUP</b>",
        "To continue, add <b>THIS exact bot</b> "
        + handle + " as an administrator of a <b>private channel or "
        "supergroup</b>. This lets it grant buyers access and revoke it "
        "when a subscription ends.",
        "<b>📋 STEP-BY-STEP GUIDE</b>",
        "<b>1️⃣ Make the bot an administrator.</b>\n"
        "Open your channel or group → tap its name → Edit / Manage → "
        "Administrators → Add Administrator. Search for "
        + handle + " and select this exact bot. "
        "<i>Adding it as an ordinary member is not enough.</i>",
        "<b>2️⃣ Enable TWO required permissions:</b>\n"
        "✅ <b>Invite users / create invite links</b> "
        "— to grant access after payment.\n"
        "✅ <b>Ban / remove users</b> "
        "— to end access when subscriptions expire.\n"
        "<i>Permission names may vary slightly by Telegram app.</i>",
        "<b>3️⃣ Confirm your channel or group in ViMi Pay.</b>\n"
        "Save the administrator permissions and return <b>to this chat</b>. "
        "Tap the button below, choose the channel or group and follow "
        "Telegram's instructions. ViMi Pay will verify your permissions "
        "and the bot's permissions.",
        "<b>🛡 YOUR SETTINGS ARE PRESERVED</b>\n"
        "Previously saved channels, plans, payment methods and other "
        "settings will remain available.",
    ])
    return ru, en


def _channel_step_rich(*, bot_name: str, timezone: str) -> str:
    ru, en = _channel_setup_copy(bot_name)
    paragraphs = tr(ru, en).split("\n")
    # Long instructions stay in distinct Rich paragraphs instead of one
    # wall of text, while explicit line breaks are kept inside each step.
    sections = []
    current: list[str] = []
    for line in paragraphs:
        if line.startswith("<b>") and (
            "1️⃣" in line or "2️⃣" in line or "3️⃣" in line
            or "📋" in line or "🛡" in line or "📢" in line
        ):
            if current:
                sections.append("<p>" + "<br>".join(current) + "</p>")
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("<p>" + "<br>".join(current) + "</p>")
    return "\n".join([
        "<h3>" + tr("✨ Настройка ViMi Pay", "✨ ViMi Pay setup") + "</h3>",
        "<p>🤖 <b>@" + escape(bot_name.lstrip("@"))
        + "</b> 🌍 " + escape(timezone) + "</p>",
        "<table bordered compact><tr><th>"
        + tr("🚀 Этап", "🚀 Step") + "</th><td><b>1/4</b></td></tr></table>",
        *sections,
        RICH_ACTIONS_MARKER,
    ])


def _channel_step_classic(*, bot_name: str, timezone: str) -> str:
    ru, en = _channel_setup_copy(bot_name)
    return "\n\n".join([
        tr("✨ <b>Настройка ViMi Pay</b>", "✨ <b>ViMi Pay setup</b>"),
        "🤖 <b>@" + escape(bot_name.lstrip("@"))
        + "</b> 🌍 " + escape(timezone),
        "<b>" + tr("🚀 Этап 1/4", "🚀 Step 1/4") + "</b>",
        tr(ru, en),
    ])


def wizard_primary_label(action: str, default_label: str) -> str:
    """One forward action on each onboarding step, no duplicate utility keys."""
    if action == "resources":
        return tr("📢 Подключить канал / группу",
                  "📢 Connect channel / group")
    return default_label


def owner_setup_step_rich(
    *, bot_name: str, timezone: str, action: str,
) -> str:
    if action == "resources":
        return _channel_step_rich(bot_name=bot_name, timezone=timezone)
    step = _STEP.get(action, 1)
    items = {
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
    title, description = items.get(action, items["methods"])
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
    if action == "resources":
        return _channel_step_classic(bot_name=bot_name, timezone=timezone)
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
