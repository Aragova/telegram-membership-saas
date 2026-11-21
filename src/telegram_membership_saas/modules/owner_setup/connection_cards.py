"""Bilingual Rich and classic cards for central bot connection lifecycle.

Presentation only: callbacks, registration, billing and bot retirement stay in
the existing owner_setup handlers. All identifiers from Telegram are escaped.
"""
from __future__ import annotations

from html import escape

from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER


def _import_note(status: str | None) -> tuple[str, str] | None:
    if status == "applied":
        return (
            tr("📁 Профиль сети применён", "📁 Network profile applied"),
            tr(
                "Тарифы добавлены как черновики, способы оплаты выключены. "
                "В своём боте привяжите канал, проверьте реквизиты и включите продажи.",
                "Plans were added as drafts and payment methods are off. "
                "In your bot, connect a channel, check the payment details "
                "and enable sales.",
            ),
        )
    if status == "failed":
        return (
            tr("⚠️ Профиль сети не скопирован", "⚠️ Network profile not copied"),
            tr(
                "Бот подключён без шаблона. Настройте его вручную.",
                "The bot is connected without a template. Set it up manually.",
            ),
        )
    return None


def connection_success_rich(*, username: str, imported: str | None) -> str:
    """Only the next step and an optional entry to central management."""
    notice = _import_note(imported)
    lines = [
        "<h3>" + tr("✅ БОТ УСПЕШНО ПОДКЛЮЧЁН!",
                    "✅ BOT CONNECTED SUCCESSFULLY!") + "</h3>",
        "<p>🤖 <b>@" + escape(username, quote=True) + "</b></p>",
        "<p>✅ <i>" + tr(
            "Бот добавлен в ViMi Pay. Теперь осталось настроить продажи.",
            "Your bot is added to ViMi Pay. Next, set up sales.",
        ) + "</i></p>",
        "<h4>" + tr("🚀 ЧТО ДЕЛАТЬ ДАЛЬШЕ?",
                     "🚀 WHAT TO DO NEXT?") + "</h4>",
        "<p><b>" + tr("1️⃣ ПЕРЕЙДИТЕ В СВОЕГО БОТА",
                      "1️⃣ OPEN YOUR BOT") + "</b><br>"
        + tr("Нажмите зелёную кнопку ниже.",
             "Tap the green button below.") + "</p>",
        "<p><b>" + tr("2️⃣ ПОДКЛЮЧИТЕ КАНАЛ",
                      "2️⃣ CONNECT A CHANNEL") + "</b><br>"
        + tr("Следуйте инструкции в боте и добавьте Telegram-канал.",
             "Follow the instructions in your bot to add a Telegram channel.")
        + "</p>",
        "<p><b>" + tr("3️⃣ НАСТРОЙТЕ ПРОДАЖИ",
                      "3️⃣ SET UP SALES") + "</b><br>"
        + tr("Добавьте способы оплаты и тарифы подписки.",
             "Add payment methods and subscription plans.") + "</p>",
    ]
    if notice is not None:
        heading, body = notice
        lines.append("<p><b>" + heading + "</b><br><i>" + body + "</i></p>")
    lines.extend([
        "<h4>" + tr("⚙️ НУЖНО ИЗМЕНИТЬ ПОДКЛЮЧЕНИЕ?",
                     "⚙️ NEED TO CHANGE THE CONNECTION?") + "</h4>",
        "<p><i>" + tr(
            "Откройте управление, чтобы проверить запуск, обновить токен, "
            "переименовать подключение или отключить бота.",
            "Open management to check the bot status, update its token, "
            "rename the connection or disconnect the bot.",
        ) + "</i></p>",
        RICH_ACTIONS_MARKER,
    ])
    return "\n".join(lines)


def connection_success_classic(*, username: str, imported: str | None) -> str:
    lines = [
        tr("✅ <b>БОТ УСПЕШНО ПОДКЛЮЧЁН!</b>",
           "✅ <b>BOT CONNECTED SUCCESSFULLY!</b>"),
        "🤖 <b>@" + escape(username, quote=True) + "</b>",
        "",
        "✅ <i>" + tr(
            "Бот добавлен в ViMi Pay. Теперь осталось настроить продажи.",
            "Your bot is added to ViMi Pay. Next, set up sales.",
        ) + "</i>",
        "",
        tr("🚀 <b>ЧТО ДЕЛАТЬ ДАЛЬШЕ?</b>",
           "🚀 <b>WHAT TO DO NEXT?</b>"),
        tr("1️⃣ <b>ПЕРЕЙДИТЕ В СВОЕГО БОТА</b>\nНажмите зелёную кнопку ниже.",
           "1️⃣ <b>OPEN YOUR BOT</b>\nTap the green button below."),
        tr("2️⃣ <b>ПОДКЛЮЧИТЕ КАНАЛ</b>\nДобавьте канал в своём боте.",
           "2️⃣ <b>CONNECT A CHANNEL</b>\nAdd a channel in your bot."),
        tr("3️⃣ <b>НАСТРОЙТЕ ПРОДАЖИ</b>\nДобавьте оплату и тарифы.",
           "3️⃣ <b>SET UP SALES</b>\nAdd payments and plans."),
    ]
    notice = _import_note(imported)
    if notice:
        lines.extend(["", "<b>" + notice[0] + "</b>\n<i>" + notice[1] + "</i>"])
    lines.extend([
        "",
        tr("⚙️ <b>НУЖНО ИЗМЕНИТЬ ПОДКЛЮЧЕНИЕ?</b>",
           "⚙️ <b>NEED TO CHANGE THE CONNECTION?</b>"),
        "<i>" + tr(
            "Кнопка управления откроет статус, токен, название и отключение.",
            "Manage opens status, token, name and disconnect actions.",
        ) + "</i>",
    ])
    return "\n".join(lines)


def _bot_status(value: str) -> str:
    return {
        "registered": tr("✅ Подключён", "✅ Connected"),
        "active": tr("🟢 Активен", "🟢 Active"),
        "deleting": tr("🟡 Отключается", "🟡 Disconnecting"),
        "disabled": tr("⚪ Отключён", "⚪ Disabled"),
        "draft": tr("📝 Черновик", "📝 Draft"),
        "archived": tr("📦 В архиве", "📦 Archived"),
    }.get(value, tr("⚪ Статус уточняется", "⚪ Status pending"))


def _action_descriptions(
    *, connected: bool, has_instances: bool, editable: bool,
    retiring: bool, danger_kind: str | None,
) -> list[tuple[str, str]]:
    items = []
    if connected:
        items.append((
            tr("🚀 Открыть бота", "🚀 Open bot"),
            tr("Перейти к настройке канала, тарифов и оплаты.",
               "Continue setting up your channel, plans and payments."),
        ))
    if has_instances:
        items.append((
            tr("🔄 Обновить статус", "🔄 Refresh status"),
            tr("Проверить текущее состояние запуска, не перезапуская бота.",
               "Check the launch status without restarting the bot."),
        ))
    if editable and not retiring:
        items.append((
            tr("🔑 Подключить / обновить токен",
               "🔑 Connect / update token"),
            tr("Указать новый токен из BotFather.",
               "Enter a new token from BotFather."),
        ))
        if connected:
            items.append((
                tr("🤖 Синхронизация Managed Bot",
                   "🤖 Managed Bot sync"),
                tr("Получить актуальный токен бота, созданного через ViMi Pay.",
                   "Fetch the current token for a bot created via ViMi Pay."),
            ))
    if editable:
        items.append((
            tr("✏️ Переименовать", "✏️ Rename"),
            tr("Изменить внутреннее название в ViMi Pay, не @username.",
               "Change its internal ViMi Pay label, not its @username."),
        ))
    items.append((
        tr("🧾 Счёт бота" if connected else "🧾 Счета аккаунта",
           "🧾 Bot bill" if connected else "🧾 Account invoices"),
        tr(
            "Пробный период, обслуживание, продажи и долг именно этого бота."
            if connected else "История счетов аккаунта, в том числе удалённых ботов.",
            "This bot's trial, service, sales and invoice balance."
            if connected else "Account invoice history, including retired bots.",
        ),
    ))
    if danger_kind == "detach":
        items.append((
            tr("🗑️ Отключить бота", "🗑️ Disconnect bot"),
            tr("Запустить защищённое отключение; история и обязательства сохранятся.",
               "Start protected disconnection; history and obligations stay."),
        ))
    elif danger_kind == "archive":
        items.append((
            tr("📦 Убрать в архив", "📦 Archive connection"),
            tr("Скрыть подключение, сохранив финансовую историю.",
               "Hide this connection while keeping financial history."),
        ))
    elif danger_kind == "delete":
        items.append((
            tr("🗑️ Удалить подключение", "🗑️ Delete connection"),
            tr("Удалить пустое подключение после отдельного подтверждения.",
               "Remove this empty connection after separate confirmation."),
        ))
    return items


def connection_management_rich(
    *, project_name: str, bots: tuple[tuple[str, str], ...],
    runtime_label: str, retiring: bool, has_instances: bool,
    editable: bool, danger_kind: str | None,
) -> str:
    bot_list = (
        "<table bordered compact>" + "".join(
            "<tr><th>🤖 @" + escape(username, quote=True)
            + "</th><td>" + _bot_status(status) + "</td></tr>"
            for username, status in bots
        ) + "</table>"
        if bots else "<p>" + tr(
            "⚪ Telegram-бот не подключён.", "⚪ No Telegram bot connected.",
        ) + "</p>"
    )
    lines = [
        "<h3>" + tr("✦ УПРАВЛЕНИЕ ПОДКЛЮЧЕНИЕМ",
                    "✦ MANAGE CONNECTION") + "</h3>",
        "<p><b>" + escape(project_name, quote=True) + "</b></p>",
        bot_list,
        "<p><b>" + tr("📡 СОСТОЯНИЕ ЗАПУСКА",
                      "📡 LAUNCH STATUS") + "</b><br>"
        + ("⏳ " + tr(
            "Бот отключается. Обновите статус после завершения.",
            "Bot is disconnecting. Refresh after completion.",
        ) if retiring else escape(runtime_label, quote=True)) + "</p>",
        "<h4>" + tr("📋 ЧТО МОЖНО СДЕЛАТЬ?",
                     "📋 WHAT CAN YOU DO HERE?") + "</h4>",
    ]
    descriptions = _action_descriptions(
        connected=any(s == "registered" for _, s in bots),
        has_instances=has_instances, editable=editable,
        retiring=retiring, danger_kind=danger_kind,
    )
    lines.extend(
        "<p><b>" + title + "</b><br><i>" + desc + "</i></p>"
        for title, desc in descriptions
    )
    lines.extend([
        "<p>⬅️ <i>" + tr(
            "«Все подключения» вернёт к списку ваших ботов.",
            "“All connections” returns to your bot list.",
        ) + "</i></p>",
        RICH_ACTIONS_MARKER,
    ])
    return "\n".join(lines)


def connection_management_classic(
    *, project_name: str, bots: tuple[tuple[str, str], ...],
    runtime_label: str, retiring: bool, has_instances: bool,
    editable: bool, danger_kind: str | None,
) -> str:
    lines = [
        tr("✦ <b>УПРАВЛЕНИЕ ПОДКЛЮЧЕНИЕМ</b>",
           "✦ <b>MANAGE CONNECTION</b>"),
        "<b>" + escape(project_name, quote=True) + "</b>",
        "",
    ]
    lines.extend(
        "🤖 <b>@" + escape(username, quote=True) + "</b> · "
        + _bot_status(status)
        for username, status in bots
    )
    if not bots:
        lines.append(tr("⚪ Telegram-бот не подключён.",
                        "⚪ No Telegram bot connected."))
    lines.extend([
        "",
        tr("📡 <b>СОСТОЯНИЕ ЗАПУСКА</b>", "📡 <b>LAUNCH STATUS</b>"),
        ("⏳ " + tr(
            "Бот отключается. Обновите статус после завершения.",
            "Bot is disconnecting. Refresh after completion.",
        ) if retiring else escape(runtime_label, quote=True)),
        "",
        tr("📋 <b>ЧТО МОЖНО СДЕЛАТЬ?</b>",
           "📋 <b>WHAT CAN YOU DO HERE?</b>"),
    ])
    lines.extend(
        "<b>" + title + "</b>\n<i>" + desc + "</i>"
        for title, desc in _action_descriptions(
            connected=any(s == "registered" for _, s in bots),
            has_instances=has_instances, editable=editable,
            retiring=retiring, danger_kind=danger_kind,
        )
    )
    lines.extend(["", "⬅️ <i>" + tr(
        "«Все подключения» вернёт к списку.",
        "“All connections” returns to your list.",
    ) + "</i>"])
    return "\n".join(lines)
