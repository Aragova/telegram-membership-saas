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
    return "primary" if action in ("resources", "methods") else "success"


def owner_setup_step_rich(
    *, bot_name: str, timezone: str, action: str,
) -> str:
    if action == "resources":
        return _channel_step_rich(bot_name=bot_name, timezone=timezone)
    if action == "saasprofile":
        # The Stars flow can create a live plan while setting up a payment
        # method. Do not present an already-completed plan stage again.
        return "\n".join([
            "<h3>" + tr(
                "✨ Завершение настройки ViMi Pay",
                "✨ Finish setting up ViMi Pay",
            ) + "</h3>",
            "<p>✅ <b>" + tr(
                "ОСНОВНЫЕ ЭТАПЫ ВЫПОЛНЕНЫ",
                "MAIN SETUP STEPS COMPLETE",
            ) + "</b></p>",
            "<p>✅ " + tr(
                "Канал или группа подключены.",
                "Channel or group connected.",
            ) + "<br>✅ " + tr(
                "Способ оплаты настроен.",
                "Payment method configured.",
            ) + "<br>✅ " + tr(
                "Первый тариф создан и активирован.",
                "First plan created and activated.",
            ) + "</p>",
            "<h4>" + tr(
                "💱 ОСТАЛСЯ ПОСЛЕДНИЙ ШАГ",
                "💱 ONE FINAL STEP",
            ) + "</h4>",
            "<p>" + tr(
                "Выберите валюту, в которой будете оплачивать "
                "<b>обслуживание ViMi Pay</b>.",
                "Choose the currency for your "
                "<b>ViMi Pay service bills</b>.",
            ) + "</p>",
            "<table bordered compact><tr><th>🇷🇺 RUB</th>"
            "<th>🇺🇸 USD</th></tr><tr><td><b>150 ₽/"
            + tr("мес.", "month") + "</b></td><td><b>$5/"
            + tr("мес.", "month") + "</b></td></tr></table>",
            "<p>" + tr(
                "Первый счёт — только обслуживание. "
                "Со следующего периода добавляется 5% подтверждённых продаж.",
                "The first bill is service-only. "
                "Later bills include 5% of confirmed sales.",
            ) + "</p>",
            "<p>➡️ " + tr(
                "После выбора валюты откроется <b>полная панель "
                "администратора</b>.",
                "After choosing a currency, the <b>full admin "
                "dashboard</b> will open.",
            ) + "</p>",
            RICH_ACTIONS_MARKER,
        ])
    if action == "methods":
        # The owner is already inside their own bot. Avoid repeating its
        # username/timezone and keep the actual 3/4 lesson short.
        return "\n".join([
            "<h3>" + tr("✨ Настройка ViMi Pay", "✨ ViMi Pay setup") + "</h3>",
            "<table bordered compact><tr><th>"
            + tr("🚀 Этап настройки", "🚀 Setup step")
            + "</th><td><b>3/4</b></td></tr></table>",
            "<h4>" + tr("💳 Способы оплаты", "💳 Payment methods") + "</h4>",
            "<p>" + tr(
                "Выберите, <b>как покупатели будут оплачивать</b> "
                "подписки и файлы.",
                "Choose <b>how buyers will pay</b> for subscriptions "
                "and files.",
            ) + "</p>",
            "<p>1️⃣ " + tr(
                "Настройте <b>минимум один способ оплаты</b>.",
                "Set up <b>at least one payment method</b>.",
            ) + "</p>",
            "<p>2️⃣ " + tr(
                "Остальные способы можно <b>добавить позже</b>.",
                "You can <b>add the other methods later</b>.",
            ) + "</p>",
            RICH_ACTIONS_MARKER,
        ])
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
    if action == "saasprofile":
        return "\n\n".join([
            tr(
                "✨ <b>Завершение настройки ViMi Pay</b>",
                "✨ <b>Finish setting up ViMi Pay</b>",
            ),
            "✅ <b>" + tr(
                "ОСНОВНЫЕ ЭТАПЫ ВЫПОЛНЕНЫ",
                "MAIN SETUP STEPS COMPLETE",
            ) + "</b>\n" + tr(
                "✅ Канал или группа подключены.\n"
                "✅ Способ оплаты настроен.\n"
                "✅ Первый тариф создан и активирован.",
                "✅ Channel or group connected.\n"
                "✅ Payment method configured.\n"
                "✅ First plan created and activated.",
            ),
            "<b>" + tr(
                "💱 ОСТАЛСЯ ПОСЛЕДНИЙ ШАГ",
                "💱 ONE FINAL STEP",
            ) + "</b>\n" + tr(
                "Выберите валюту обслуживания ViMi Pay.",
                "Choose your ViMi Pay service currency.",
            ),
            "🇷🇺 <b>RUB · 150 ₽/"
            + tr("мес.", "month") + "</b>\n"
            + "🇺🇸 <b>USD · $5/"
            + tr("мес.", "month") + "</b>",
            tr(
                "Первый счёт — обслуживание. Со следующего периода "
                "добавляется 5% подтверждённых продаж.",
                "First bill: service only. Later bills include "
                "5% of confirmed sales.",
            ),
            tr(
                "После выбора валюты откроется полная панель администратора.",
                "After choosing a currency, the full admin dashboard opens.",
            ),
        ])
    if action == "resources":
        return _channel_step_classic(bot_name=bot_name, timezone=timezone)
    if action == "methods":
        return "\n\n".join([
            tr("✨ <b>Настройка ViMi Pay</b>", "✨ <b>ViMi Pay setup</b>"),
            tr("🚀 <b>Этап 3/4 · Способы оплаты</b>",
               "🚀 <b>Step 3/4 · Payment methods</b>"),
            tr("Выберите, <b>как покупатели будут оплачивать</b> "
               "подписки и файлы.",
               "Choose <b>how buyers will pay</b> for subscriptions and files."),
            tr("1️⃣ Настройте <b>минимум один способ оплаты</b>.\n"
               "2️⃣ Остальные можно <b>добавить позже</b>.",
               "1️⃣ Set up <b>at least one payment method</b>.\n"
               "2️⃣ You can <b>add the others later</b>."),
        ])
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
