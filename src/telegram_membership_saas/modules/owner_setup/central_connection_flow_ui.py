"""Approved bilingual Rich/classic UX for central bot connection workflows.

Presentation only. Real authorization, retirement and input lifecycle remain
in owner_setup handlers and services.
"""
from __future__ import annotations

from html import escape

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.bots.deletion_service import RetirementBlockers


def _card(title: str, paragraphs: list[str], *, rich: bool) -> str:
    if rich:
        return "\n".join(["<h3>" + title + "</h3>", *(
            "<p>" + paragraph.replace("\n", "<br>") + "</p>"
            for paragraph in paragraphs
        )])
    return "\n\n".join(["<b>" + title + "</b>", *paragraphs])


def token_input_card(*, username: str | None, rich: bool) -> str:
    identity = ("🤖 <b>@" + escape(username.lstrip("@"), quote=True) + "</b>"
                if username else tr("🤖 <b>Новое подключение</b>",
                                     "🤖 <b>New connection</b>"))
    return _card(
        tr("🔑 ОБНОВЛЕНИЕ ТОКЕНА", "🔑 UPDATE BOT TOKEN"),
        [
            identity,
            "<b>" + tr("🔐 ЗАЧЕМ ЭТО НУЖНО?", "🔐 WHY UPDATE?") + "</b>\n"
            + tr("Если вы заменили токен в @BotFather или старый перестал работать, "
                 "укажите новый здесь.",
                 "If you rotated your token in @BotFather or the old one stopped "
                 "working, connect the replacement here."),
            "<b>" + tr("📋 КАК ОБНОВИТЬ?", "📋 HOW TO UPDATE") + "</b>\n"
            + tr("1️⃣ Откройте @BotFather и выберите своего бота.\n"
                 "2️⃣ Откройте API Token и получите новый токен.\n"
                 "3️⃣ ➡️ ОТПРАВЬТЕ ТОКЕН ПРЯМО СЮДА, обычным сообщением "
                 "в этот чат с ViMi Pay.",
                 "1️⃣ Open @BotFather and select your bot.\n"
                 "2️⃣ Open API Token and get the new token.\n"
                 "3️⃣ ➡️ SEND THE TOKEN RIGHT HERE as a regular message "
                 "in this ViMi Pay chat."),
            "<b>" + tr("🛡 БЕЗОПАСНОСТЬ", "🛡 SECURITY") + "</b>\n"
            + tr("ViMi Pay удалит сообщение с токеном и проверит подключение. "
                 "Не отправляйте токен другим людям.",
                 "ViMi Pay will delete the token message and verify the connection. "
                 "Never share your token with other people."),
        ], rich=rich,
    )


def rename_input_card(*, username: str | None, rich: bool) -> str:
    identity = ("🤖 <b>@" + escape(username.lstrip("@"), quote=True) + "</b>"
                if username else tr("🤖 <b>Подключение</b>",
                                     "🤖 <b>Connection</b>"))
    return _card(
        tr("✏️ НАЗВАНИЕ ПОДКЛЮЧЕНИЯ", "✏️ CONNECTION NAME"),
        [
            identity,
            "<b>" + tr("📝 КАК ЭТО РАБОТАЕТ?", "📝 HOW DOES IT WORK?") + "</b>\n"
            + tr("Дайте подключению удобное название, чтобы находить его "
                 "среди других ботов. Оно видно только вам в ViMi Pay. "
                 "Имя и @username Telegram-бота не изменятся.",
                 "Give this connection a name to find it among your bots. "
                 "Only you see it in ViMi Pay. The bot's Telegram name and "
                 "@username do not change."),
            "<b>" + tr("✍️ ВВЕДИТЕ НОВОЕ НАЗВАНИЕ",
                       "✍️ ENTER A NEW NAME") + "</b>\n"
            + tr("➡️ Отправьте его прямо сюда обычным сообщением. "
                 "От 2 до 120 символов.",
                 "➡️ Send it right here as a regular message. "
                 "Use 2 to 120 characters."),
        ], rich=rich,
    )


def _customer_counts(blockers: RetirementBlockers) -> list[str]:
    return [
        tr("💳 Покупки ждут проверки", "💳 Purchases need review")
        + ": <b>" + str(blockers.requests) + "</b>",
        tr("🎟 Действующие подписки", "🎟 Active subscriptions")
        + ": <b>" + str(blockers.subscriptions) + "</b>",
        tr("📨 Ожидают приглашения", "📨 Waiting for invitations")
        + ": <b>" + str(blockers.join_tickets) + "</b>",
    ]


def disconnect_card(*, username: str, blockers: RetirementBlockers,
                    rich: bool) -> str:
    title = tr("🗑 ОТКЛЮЧЕНИЕ БОТА", "🗑 DISCONNECT BOT")
    parts = [
        "🤖 <b>@" + escape(username.lstrip("@"), quote=True) + "</b>",
        "<b>⚠️ " + tr("ВЫ СОБИРАЕТЕСЬ ОТКЛЮЧИТЬ БОТА ОТ ViMi Pay.",
                       "YOU ARE ABOUT TO DISCONNECT THIS BOT FROM ViMi Pay.")
        + "</b>",
        "<b>" + tr("📌 ЧТО ПРОИЗОЙДЁТ?", "📌 WHAT HAPPENS NEXT?") + "</b>\n"
        + tr("🛑 Новые продажи через ViMi Pay прекратятся.\n"
             "💾 Настройки и история платежей сохранятся.\n"
             "🤖 Telegram-бот в @BotFather не удаляется. "
             "Повторно подключить его можно после завершения отключения.",
             "🛑 New purchases through ViMi Pay will stop.\n"
             "💾 Settings and payment history will remain.\n"
             "🤖 The Telegram bot is not deleted from @BotFather. "
             "You can reconnect it when disconnection is complete."),
        "<b>" + tr("🔍 ПРОВЕРКА ПЕРЕД ОТКЛЮЧЕНИЕМ",
                    "🔍 PRE-DISCONNECTION CHECK") + "</b>\n"
        + "\n".join(_customer_counts(blockers)),
    ]
    if blockers.has_blockers:
        parts.append(
            "<b>⛔ " + tr("ОТКЛЮЧЕНИЕ ПОКА НЕДОСТУПНО.",
                          "DISCONNECTION IS NOT AVAILABLE YET.") + "</b>\n"
            + tr("Остались покупатели или неоконченные действия по доступу. "
                 "Откройте «Покупатели и доступы», чтобы разобраться. "
                 "Оплаченный доступ нельзя прекращать без урегулирования "
                 "вопроса с покупателем.",
                 "There are customers or pending access actions. "
                 "Open Customers and access to resolve them. "
                 "Do not interrupt paid access without settling it "
                 "with the customer.")
        )
    else:
        parts.append("<b>✅ " + tr(
            "Проверка пройдена. Бота можно безопасно отключить.",
            "Checks passed. The bot can be disconnected safely.",
        ) + "</b>")
    return _card(title, parts, rich=rich)


def obligations_card(*, username: str | None, blockers: RetirementBlockers,
                     section: str, page: int, rich: bool) -> str:
    identity = ("🤖 <b>@" + escape(username.lstrip("@"), quote=True) + "</b>"
                if username else tr("🤖 <b>Это подключение</b>",
                                     "🤖 <b>This connection</b>"))
    if section == "overview":
        parts = [
            identity,
            "<b>" + tr("🛡 ПОЧЕМУ ПОКА НЕЛЬЗЯ ОТКЛЮЧИТЬ?",
                        "🛡 WHY CAN'T I DISCONNECT YET?") + "</b>\n"
            + tr("Остались покупки, подписки или неоконченные действия по "
                 "доступу. Разберитесь с ними, чтобы покупатели не потеряли "
                 "оплаченный доступ.",
                 "Purchases, subscriptions or access changes are still open. "
                 "Resolve them first so customers do not lose paid access."),
            "<b>" + tr("📊 ЧТО НУЖНО ПРОВЕРИТЬ?",
                        "📊 WHAT NEEDS ATTENTION?") + "</b>\n"
            + "\n".join(_customer_counts(blockers)),
        ]
        other = blockers.grants + blockers.access + blockers.access_actions
        if other:
            parts.append(
                tr("🔑 Другие доступы и незавершённые действия",
                   "🔑 Other access and pending actions")
                + ": <b>" + str(other) + "</b>"
            )
        parts.append("<b>" + tr("📌 ЧТО ДЕЛАТЬ ДАЛЬШЕ?",
                                 "📌 WHAT TO DO NEXT?") + "</b>\n"
            + tr("1️⃣ Проверьте покупки и подтвердите или отклоните оплаты.\n"
                 "2️⃣ Проверьте действующие подписки и урегулируйте "
                 "оплаченный срок с покупателями.\n"
                 "3️⃣ Когда все вопросы решены, вернитесь к отключению.",
                 "1️⃣ Review open purchases and confirm or reject payments.\n"
                 "2️⃣ Review active subscriptions and settle any remaining "
                 "paid term with customers.\n"
                 "3️⃣ Return to disconnection when all issues are resolved."))
        if blockers.has_blockers:
            parts.append("<b>⚠️ " + tr(
                "Пока есть открытые вопросы, бот не отключится.",
                "The bot cannot be disconnected while issues remain.",
            ) + "</b>")
        else:
            parts.append("<b>✅ " + tr(
                "Открытых вопросов больше нет. Можно вернуться к отключению.",
                "No outstanding issues. You can return to disconnection.",
            ) + "</b>")
    else:
        labels = {
            "pay": tr("💳 ПОКУПКИ НА ПРОВЕРКЕ", "💳 PURCHASES TO REVIEW"),
            "sub": tr("🎟 ДЕЙСТВУЮЩИЕ ПОДПИСКИ", "🎟 ACTIVE SUBSCRIPTIONS"),
            "grant": tr("🔑 ДРУГИЕ ДОСТУПЫ", "🔑 OTHER ACCESS"),
            "sync": tr("🔄 СОСТОЯНИЕ ДОСТУПОВ", "🔄 ACCESS STATUS"),
        }
        parts = [
            identity,
            "<b>" + labels[section] + "</b>",
            tr("📄 Страница ", "📄 Page ") + str(page + 1),
            tr("Выберите конкретную запись ниже. Каждое изменение потребует "
               "отдельного подтверждения. История платежей сохранится.",
               "Choose an item below. Each change needs separate "
               "confirmation. Payment history is preserved."),
        ]
        if section == "sub":
            parts.append("⚠️ " + tr(
                "Не завершайте оплаченный срок без согласования с покупателем. "
                "ViMi Pay не делает возврат автоматически.",
                "Do not end a paid term without resolving it with the customer. "
                "ViMi Pay does not refund automatically.",
            ))
        if section == "sync":
            parts.append("🔄 " + tr(
                "ViMi Pay должен подтвердить фактический доступ в Telegram. "
                "Непроверенные действия не помечаются как выполненные.",
                "ViMi Pay must verify real Telegram access. "
                "Unconfirmed actions are never marked completed.",
            ))
    return _card(tr("👥 ПОКУПАТЕЛИ И ДОСТУПЫ",
                    "👥 CUSTOMERS AND ACCESS"), parts, rich=rich)
