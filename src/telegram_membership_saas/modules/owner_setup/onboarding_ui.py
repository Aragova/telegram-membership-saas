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
    # Step 2 is the channel/group selection screen.
    "methods": 3,
    "plans": 4,
    # Service currency is a final confirmation after the 4 guided steps.
    "saasprofile": 4,
}


def _channel_setup_copy(bot_name: str) -> tuple[str, str]:
    """Concise RU/EN setup copy for the exact connected bot.

    Telegram verification still requires can_invite_users and
    can_restrict_members; this only shortens the instructions.
    """
    handle = "<b>@" + escape(bot_name.lstrip("@")) + "</b>"
    ru = "\n".join([
        "<b>📢 ПОДКЛЮЧЕНИЕ КАНАЛА</b>",
        "Добавьте <b>ЭТОГО бота</b> " + handle
        + " в администраторы вашего <b>закрытого канала или группы</b>.",
        "<b>📋 КАК ЭТО СДЕЛАТЬ?</b>",
        "<b>1️⃣</b> Откройте канал или группу → <b>Управление</b> → "
        "<b>Администраторы</b> → <b>Добавить администратора</b>.",
        "<b>2️⃣</b> Найдите " + handle + " и включите <b>два права</b>:\n"
        "✅ <b>Добавление пользователей</b> (пригласительные ссылки)\n"
        "✅ <b>Блокировка / удаление пользователей</b>",
        "<b>3️⃣</b> Сохраните права и нажмите кнопку ниже для подключения.",
        "<b>🛡</b> Сохранённые тарифы, оплаты и настройки не изменятся.",
    ])
    en = "\n".join([
        "<b>📢 CONNECT A CHANNEL</b>",
        "Add <b>THIS bot</b> " + handle
        + " as an administrator of your <b>private channel or group</b>.",
        "<b>📋 HOW TO CONNECT?</b>",
        "<b>1️⃣</b> Open the channel or group → <b>Manage</b> → "
        "<b>Administrators</b> → <b>Add Administrator</b>.",
        "<b>2️⃣</b> Find " + handle + " and enable <b>two permissions</b>:\n"
        "✅ <b>Add users</b> (invite links)\n"
        "✅ <b>Ban / remove users</b>",
        "<b>3️⃣</b> Save the permissions and tap the button below to connect.",
        "<b>🛡</b> Saved plans, payments and settings will not change.",
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
    if action == "methods":
        return tr("💳 Настроить способы оплаты", "💳 Set up payment methods")
    if action == "plans":
        return tr("📦 Создать первый тариф", "📦 Create first plan")
    if action == "saasprofile":
        return tr("💱 Завершить настройку", "💱 Finish setup")
    return default_label


def wizard_primary_style(action: str) -> str:
    """Blue connect button on step 1/4; other wizard actions unchanged."""
    return "primary" if action == "resources" else "success"


def owner_setup_step_rich(
    *, bot_name: str, timezone: str, action: str,
) -> str:
    if action == "resources":
        return _channel_step_rich(bot_name=bot_name, timezone=timezone)
    step = _STEP.get(action, 1)
    items = {
        "methods": (
            tr("💳 Способы оплаты", "💳 Payment methods"),
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
    progress = (tr("Завершение", "Finish") if action == "saasprofile"
                else str(step) + "/4")
    return "\n".join([
        "<h3>" + tr("✨ Настройка ViMi Pay", "✨ ViMi Pay setup") + "</h3>",
        "<p><b>" + escape(bot_name) + "</b> 🌍 " + escape(timezone) + "</p>",
        "<table bordered compact><tr><th>" + tr(
            "Этап", "Step",
        ) + "</th><td>" + progress + "</td></tr></table>",
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
    progress = (tr("Завершение", "Finish") if action == "saasprofile"
                else str(step) + "/4")
    return (
        tr("✨ <b>Настройка ViMi Pay</b>", "✨ <b>ViMi Pay setup</b>")
        + "\n🤖 <b>" + escape(bot_name) + "</b> 🌍 "
        + escape(timezone) + "\n"
        + tr("Этап ", "Step ") + progress + "\n\n"
        + tr("Следуйте кнопке ниже для продолжения. Все настройки "
             "сохраняются.", "Use the button below to continue. Your "
             "progress is saved.")
    )
