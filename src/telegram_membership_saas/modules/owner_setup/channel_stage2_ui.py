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


def directory_rich(count: int, *, onboarding: bool = False) -> str:
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
    elif onboarding:
        parts.append("<p>✅ " + tr(
            "Канал или группа уже добавлены. Вернитесь назад, "
            "чтобы продолжить настройку.",
            "A channel or group is connected. Go back to continue setup.",
        ) + "</p>")
    else:
        parts.append("<p>🔗 " + tr(
            "Выберите подключённый канал или группу для управления "
            "или добавьте новый ниже.",
            "Select a channel or group to manage, "
            "or add another below.",
        ) + "</p>")
    parts.append(RICH_ACTIONS_MARKER)
    return "\n".join(parts)


def directory_classic(count: int, *, onboarding: bool = False) -> str:
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
    elif onboarding:
        parts.append("✅ " + tr(
            "Канал или группа уже добавлены. Вернитесь назад, "
            "чтобы продолжить настройку.",
            "A channel or group is connected. Go back to continue setup.",
        ))
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
    """Readable Rich card with separate headings, lists and example."""
    return "\n".join([
        "<h3>" + tr("✍️ Подключение по ID", "✍️ Connect by ID") + "</h3>",
        "<table bordered compact><tr><th>"
        + tr("🚀 Этап", "🚀 Step") + "</th><td><b>2/4</b></td></tr></table>",
        "<h4>" + tr("📨 ЧТО ОТПРАВИТЬ?", "📨 WHAT TO SEND?") + "</h4>",
        "<p>" + tr("Отправьте <b>в этот чат</b> один из вариантов:",
                    "Send <b>one of these</b> to this chat:") + "</p>",
        "<ul><li>" + tr("ID канала или группы (начинается с <code>-100</code>)",
                         "Channel or group ID (starts with <code>-100</code>)")
        + "</li><li>" + tr("Публичный <b>@username</b> канала или группы",
                           "Public channel or group <b>@username</b>")
        + "</li></ul>",
        "<h4>" + tr("✅ ПЕРЕД ОТПРАВКОЙ", "✅ BEFORE SENDING") + "</h4>",
        "<p>" + tr("Добавьте ", "Add ") + _handle(username)
        + tr(" в администраторы выбранного канала или группы.",
             " as an administrator of the selected channel or group.") + "</p>",
        "<ul><li>" + tr("Добавление пользователей", "Invite users")
        + "</li><li>" + tr("Блокировка / удаление пользователей",
                           "Ban / remove users") + "</li></ul>",
        "<h4>" + tr("💬 ПРИМЕР", "💬 EXAMPLE") + "</h4>",
        "<pre>-1001234567890\n" + tr("или", "or")
        + "\n@my_private_channel</pre>",
        RICH_ACTIONS_MARKER,
    ])


def manual_classic(username: str | None) -> str:
    return "\n\n".join([
        tr("✍️ <b>Подключение по ID</b>", "✍️ <b>Connect by ID</b>"),
        "<b>" + tr("🚀 Этап 2 из 4", "🚀 Step 2 of 4") + "</b>",
        tr("📨 <b>ЧТО ОТПРАВИТЬ?</b>", "📨 <b>WHAT TO SEND?</b>")
        + "\n" + tr(
            "Отправьте в этот чат ID канала или группы "
            "(начинается с <code>-100</code>) или публичный <b>@username</b>.",
            "Send a channel/group ID (starts with <code>-100</code>) "
            "or public <b>@username</b> to this chat.",
        ),
        tr("✅ <b>ПЕРЕД ОТПРАВКОЙ</b>", "✅ <b>BEFORE SENDING</b>")
        + "\n" + _handle(username) + tr(
            " должен быть администратором с правами:\n"
            "• Добавление пользователей\n"
            "• Блокировка / удаление пользователей",
            " must be an administrator with:\n"
            "• Invite users\n"
            "• Ban / remove users",
        ),
        tr("💬 <b>ПРИМЕР</b>", "💬 <b>EXAMPLE</b>")
        + "\n<code>-1001234567890</code> "
        + tr("или", "or") + " <code>@my_private_channel</code>",
    ])


def connection_result_rich(
    *, success: bool, kind: str, username: str | None,
    resource_name: str | None = None, onboarding: bool = False,
) -> str:
    """One result card, one forward/back action; no duplicate directory."""
    if kind not in ("channel", "group", "chat"):
        kind = "chat"
    thing = (tr("КАНАЛ", "CHANNEL") if kind == "channel"
             else tr("ГРУППА", "GROUP") if kind == "group"
             else tr("КАНАЛ / ГРУППА", "CHANNEL / GROUP"))
    head = (tr("✅ ПОДКЛЮЧЕНО", "✅ CONNECTED") if success
            else tr("⚠️ НЕ ПОДКЛЮЧЕНО", "⚠️ NOT CONNECTED"))
    parts = ["<h3>" + head + " · " + thing + "</h3>"]
    if success:
        parts.append("<p>✅ <b>" + escape(resource_name or "", quote=True)
                     + "</b></p>")
        parts.append("<p>" + tr("Подключение сохранено в ViMi Pay.",
                                "Connection saved in ViMi Pay.") + "</p>")
        parts.append("<p>🚀 " + (
            tr("Следующий этап — <b>оплата покупателей</b>.",
               "Next: <b>buyer payments</b>.")
            if onboarding else
            tr("Подключение доступно в разделе «Каналы и группы».",
               "Manage this connection under Channels and groups.")
        ) + "</p>")
    else:
        parts.extend([
            "<p>" + tr(
                "ViMi Pay не смог подтвердить доступ к выбранному чату.",
                "ViMi Pay could not verify access to this chat.",
            ) + "</p>",
            "<h4>" + tr("📋 КАК ИСПРАВИТЬ?", "📋 HOW TO FIX IT?") + "</h4>",
            "<p><b>1️⃣</b> " + tr("Добавьте ", "Add ") + _handle(username)
            + tr(" в администраторы.", " as an administrator.") + "</p>",
            "<p><b>2️⃣</b> " + tr(
                "Включите права <b>добавлять</b> и <b>блокировать пользователей</b>.",
                "Enable <b>invite users</b> and <b>ban/remove users</b>.",
            ) + "</p>",
            "<p><b>3️⃣</b> " + tr(
                "Сохраните права и повторите выбор.",
                "Save the permissions and select the chat again.",
            ) + "</p>",
        ])
    parts.append(RICH_ACTIONS_MARKER)
    return "\n".join(parts)


def connection_result_classic(
    *, success: bool, kind: str, username: str | None,
    resource_name: str | None = None, onboarding: bool = False,
) -> str:
    if kind not in ("channel", "group", "chat"):
        kind = "chat"
    thing = (tr("канал", "channel") if kind == "channel"
             else tr("группу", "group") if kind == "group"
             else tr("канал или группу", "channel or group"))
    if success:
        return "\n\n".join([
            "✅ <b>" + tr("Подключено: ", "Connected: ")
            + escape(resource_name or "", quote=True) + "</b>",
            tr("Подключение сохранено в ViMi Pay.",
               "Connection saved in ViMi Pay."),
            (tr("🚀 Следующий этап — оплата покупателей.",
                "🚀 Next: buyer payments.")
             if onboarding else tr(
                "📂 Управление доступно в разделе «Каналы и группы».",
                "📂 Manage it under Channels and groups.",
             )),
        ])
    return "\n\n".join([
        "⚠️ <b>" + tr("Не удалось подключить: ",
                      "Could not connect: ") + thing + "</b>",
        tr("ViMi Pay не смог проверить доступ.",
           "ViMi Pay could not verify access."),
        tr("📋 <b>Что сделать?</b>", "📋 <b>What to do?</b>")
        + "\n<b>1️⃣</b> " + tr("Добавьте ", "Add ") + _handle(username)
        + tr(" в администраторы.", " as an administrator.")
        + "\n<b>2️⃣</b> " + tr(
            "Разрешите добавлять и блокировать пользователей.",
            "Enable invite and ban user permissions.",
        ) + "\n<b>3️⃣</b> " + tr(
            "Повторите выбор.", "Try selecting the chat again.",
        ),
    ])
