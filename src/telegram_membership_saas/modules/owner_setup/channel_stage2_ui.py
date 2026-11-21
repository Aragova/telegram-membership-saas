"""Bilingual channel/group onboarding stage 2/4 presentation.

Only UI. Telegram chat chooser is a *native reply-keyboard* request_chat
control, so those messages use classic Telegram HTML rather than pretending
an inline Rich action can open a system chat picker.
"""
from __future__ import annotations

from html import escape

from telegram_membership_saas.localization import tr
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER


def _handle(username: str | None) -> str:
    if not username:
        return tr("этого бота", "this bot")
    return "<b>@" + escape(username.lstrip("@"), quote=True) + "</b>"


def directory_rich(count: int) -> str:
    parts = [
        "<h3>" + tr("📢 Мои каналы и группы",
                    "📢 My channels and groups") + "</h3>",
        "<table bordered compact><tr><th>"
        + tr("Подключено", "Connected") + "</th><td>"
        + str(count) + "</td></tr></table>",
    ]
    if count == 0:
        parts += [
            "<p>🔗 " + tr(
                "Выберите, что хотите подключить к ViMi Pay. "
                "Добавьте канал или группу, где бот будет выдавать "
                "и отключать доступ подписчикам.",
                "Choose what to connect to ViMi Pay. Add a channel or "
                "group where the bot will grant and revoke subscriber access.",
            ) + "</p>",
            "<p>ℹ️ " + tr(
                "После завершения первоначальной настройки откроется "
                "полная панель управления.",
                "The full management dashboard will become available "
                "when all initial setup steps are complete.",
            ) + "</p>",
        ]
    else:
        parts.append("<p>🔗 " + tr(
            "Выберите подключённый канал или группу для управления "
            "или добавьте новый ниже.",
            "Select a channel or group to manage, "
            "or add another below.",
        ) + "</p>")
    parts.append(RICH_ACTIONS_MARKER)
    return "\n".join(parts)


def directory_classic(count: int) -> str:
    parts = [
        tr("📢 <b>Мои каналы и группы</b>",
           "📢 <b>My channels and groups</b>"),
        tr("Подключено: ", "Connected: ") + str(count),
    ]
    if count == 0:
        parts += [
            "🔗 " + tr(
                "Выберите, что хотите подключить к ViMi Pay. "
                "Добавьте канал или группу, где бот будет выдавать "
                "и отключать доступ подписчикам.",
                "Choose what to connect to ViMi Pay. Add a channel "
                "or group where the bot grants and revokes access.",
            ),
            "ℹ️ " + tr(
                "После завершения первоначальной настройки откроется "
                "полная панель управления.",
                "The full dashboard will open after all setup steps.",
            ),
        ]
    else:
        parts.append("🔗 " + tr(
            "Выберите подключённый канал или группу для управления "
            "или добавьте новый ниже.",
            "Select a channel or group to manage, "
            "or add another below.",
        ))
    return "\n\n".join(parts)


def picker_classic(kind: str, username: str | None) -> str:
    """Native Telegram request_chat keyboard needs normal HTML message text."""
    if kind not in ("channel", "group"):
        raise ValueError("Unsupported chat kind")
    title = (tr("📢 <b>ПОДКЛЮЧЕНИЕ КАНАЛА</b>",
                "📢 <b>CONNECT A CHANNEL</b>")
             if kind == "channel" else
             tr("👥 <b>ПОДКЛЮЧЕНИЕ ГРУППЫ</b>",
                "👥 <b>CONNECT A GROUP</b>"))
    object_name = (tr("канал", "channel") if kind == "channel"
                   else tr("группу", "group"))
    return "\n\n".join([
        title,
        "<b>" + tr("🚀 Этап 2 из 4", "🚀 Step 2 of 4") + "</b>",
        "🔗 " + tr("Выберите ", "Choose ") + object_name + tr(
            " с помощью кнопки ниже.",
            " using the button below.",
        ),
        "✅ " + tr(
            "Перед выбором убедитесь: ", "Before selecting, make sure "
        ) + _handle(username) + tr(
            " уже назначен администратором, а права "
            "<b>добавления</b> и <b>блокировки пользователей</b> включены.",
            " is already an administrator and has both "
            "<b>invite</b> and <b>ban users</b> permissions.",
        ),
        "ℹ️ " + tr(
            "Telegram откроет системное окно выбора.",
            "Telegram will open its built-in chat picker.",
        ),
    ])


def manual_rich(username: str | None) -> str:
    handle = _handle(username)
    return "\n".join([
        "<h3>" + tr("✍️ Подключение по ID",
                    "✍️ Connect by ID") + "</h3>",
        "<table bordered compact><tr><th>"
        + tr("🚀 Этап", "🚀 Step") + "</th><td><b>2/4</b></td></tr></table>",
        "<p>🔗 " + tr(
            "Отправьте <b>ID канала или группы</b> в формате "
            "<code>-100...</code> либо публичный <b>@username</b> "
            "обычным сообщением <b>в этот чат</b>.",
            "Send the <b>channel or group ID</b> in the "
            "<code>-100...</code> format, or its public "
            "<b>@username</b>, as a regular message <b>in this chat</b>.",
        ) + "</p>",
        "<p>ℹ️ " + tr(
            "Перед отправкой убедитесь, что ", "Before sending, make sure "
        ) + handle + tr(
            " уже администратор нужного канала или группы "
            "с правами <b>добавления</b> и <b>блокировки пользователей</b>.",
            " is already an administrator of the channel or group "
            "with <b>invite</b> and <b>ban users</b> permissions.",
        ) + "</p>",
        "<p><b>" + tr("💬 Пример", "💬 Example")
        + ":</b> <code>-1001234567890</code> "
        + tr("или", "or") + " <code>@my_private_channel</code></p>",
        RICH_ACTIONS_MARKER,
    ])


def manual_classic(username: str | None) -> str:
    handle = _handle(username)
    return "\n\n".join([
        tr("✍️ <b>ПОДКЛЮЧЕНИЕ ПО ID</b>",
           "✍️ <b>CONNECT BY ID</b>"),
        "<b>" + tr("🚀 Этап 2 из 4", "🚀 Step 2 of 4") + "</b>",
        "🔗 " + tr(
            "Отправьте ID канала или группы <code>-100...</code> "
            "либо публичный <b>@username</b> прямо в этот чат.",
            "Send the channel/group ID <code>-100...</code> "
            "or public <b>@username</b> directly in this chat.",
        ),
        "ℹ️ " + tr(
            "Убедитесь, что ", "Make sure "
        ) + handle + tr(
            " уже администратор с правами добавления и блокировки пользователей.",
            " is an administrator with invite and ban permissions.",
        ),
        "💬 <b>" + tr("Пример", "Example") + ":</b> "
        + "<code>-1001234567890</code> "
        + tr("или", "or") + " <code>@my_private_channel</code>",
    ])
