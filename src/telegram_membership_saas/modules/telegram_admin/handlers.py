"""Telegram owner/admin navigation for the currently configured project."""

from __future__ import annotations

from html import escape
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.bots.service import (
    TOKEN_PATTERN,
    register_customer_bot,
    require_bot_owner,
)
from telegram_membership_saas.modules.telegram_admin.service import (
    AdminEntry,
    AdminProjectSnapshot,
    change_project_timezone,
    load_admin_project,
)
from telegram_membership_saas.modules.telegram_commerce.handlers import (
    _review_keyboard,
    _review_text,
    _send_catalog,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    list_pending_reviews_for_actor,
)

TIMEZONE_CHOICES = (
    "Europe/Moscow",
    "Europe/Amsterdam",
    "Europe/London",
    "Asia/Bangkok",
    "Asia/Dubai",
    "America/New_York",
)
SCREENS = frozenset({"main", "resources", "plans", "methods", "settings", "timezone", "bots"})
CONNECT_PROMPT_PREFIX = "Подключение собственного бота"
CONNECT_PROMPT_PREFIX_EN = "Connect your own bot"
STATUS_LABELS = {
    "active": "🟢 Активно",
    "disabled": "⚪ Отключено",
    "archived": "📦 Архив",
    "draft": "📝 Черновик",
    "registered": "⏳ Зарегистрирован",

}


ADMIN_BUTTON_LABELS_EN = {
    "📦 Тарифы": "📦 Plans",
    "📢 Каналы / группы": "📢 Channels / groups",
    "💳 Оплата": "💳 Payments",
    "⚙️ Настройки": "⚙️ Settings",
    "🤖 Мои боты": "🤖 My bots",
    "🧾 Заявки": "🧾 Payment reviews",
    "👁 Посмотреть как клиент": "👁 Buyer preview",
    "➕ Подключить своего бота": "➕ Connect my bot",
    "⬅️ Главное меню": "⬅️ Main menu",
    "🌍 Изменить часовой пояс": "🌍 Change timezone",
    "⬅️ Настройки": "⬅️ Settings",
}
STATUS_LABELS_EN = {
    "active": "🟢 Active",
    "disabled": "⚪ Disabled",
    "archived": "📦 Archived",
    "draft": "📝 Draft",
    "registered": "⏳ Registered",
}


def _button(text: str, callback_data: str, *, style: str = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=tr(text, ADMIN_BUTTON_LABELS_EN.get(text, text)),
        callback_data=callback_data, style=style,
    )


def _keyboard(screen: str, snapshot: AdminProjectSnapshot) -> InlineKeyboardMarkup:
    if screen == "main":
        rows = [
            [
                _button("📦 Тарифы", "adm:plans"),
                _button("📢 Каналы / группы", "adm:resources"),
            ],
            [
                _button("💳 Оплата", "adm:methods"),
                _button("⚙️ Настройки", "adm:settings"),
            ],
            [_button("🤖 Мои боты", "adm:bots")],
            [_button("🧾 Заявки", "adm:reviews")],
            [_button("👁 Посмотреть как клиент", "adm:client")],
            [InlineKeyboardButton(text="🌐 Language / Язык", callback_data="lng:menu")],
        ]
    elif screen == "bots":
        rows = [
            [_button("➕ Подключить своего бота", "adm:connect", style="success")],
            [_button("⬅️ Главное меню", "adm:main")],
        ]
    elif screen == "settings":
        rows = [
            [_button("🌍 Изменить часовой пояс", "adm:timezone")],
            [_button("⬅️ Главное меню", "adm:main")],
        ]
    elif screen == "timezone":
        rows = [
            [
                _button(
                    name,
                    f"adm:tz:{name}",
                    style="success" if name == snapshot.timezone else "primary",
                )
            ]
            for name in TIMEZONE_CHOICES
        ]
        rows.append([_button("⬅️ Настройки", "adm:settings")])
    else:
        rows = [[_button("⬅️ Главное меню", "adm:main")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_entries(title: str, entries: tuple[AdminEntry, ...]) -> str:
    if not entries:
        return f"<b>{title}</b>\n\n" + tr("Записей пока нет.", "No records yet.")
    lines = [f"<b>{title}</b>", ""]
    for entry in entries[:15]:
        state = tr(
            STATUS_LABELS.get(entry.status, escape(entry.status)),
            STATUS_LABELS_EN.get(entry.status, escape(entry.status)),
        )
        details_en = {
            "Канал": "Channel",
            "Группа": "Group",
            "ожидает запуска": "waiting to start",
        }
        details = tr(entry.details, details_en.get(entry.details, entry.details))
        suffix = f" · {escape(details)}" if details else ""
        lines.append(f"• <b>{escape(entry.name)}</b>{suffix}\n  {state}")
    if len(entries) > 15:
        lines.append(tr(f"\nПоказаны первые 15 из {len(entries)}.",
                        f"\nShowing the first 15 of {len(entries)}."))
    return "\n".join(lines)


def _screen_text(screen: str, snapshot: AdminProjectSnapshot) -> str:
    if screen == "main":
        return (
            tr("👑 <b>ViMi Pay · Управление</b>\n\n",
               "👑 <b>ViMi Pay · Management</b>\n\n")
            + tr("📁 Проект: ", "📁 Project: ")
            + f"<b>{escape(snapshot.project_name)}</b>\n"
            + tr("🌍 Часовой пояс: ", "🌍 Timezone: ")
            + f"<b>{escape(snapshot.timezone)}</b>\n\n"
            + tr("Каналов и групп: ", "Channels and groups: ")
            + f"{len(snapshot.resources)}\n"
            + tr("Тарифов: ", "Plans: ") + f"{len(snapshot.plans)}\n"
            + tr("Способов оплаты: ", "Payment methods: ")
            + f"{len(snapshot.payment_methods)}\n"
            + tr("Подключённых ботов: ", "Connected bots: ")
            + f"{len(snapshot.bots)}\n\n"
            + tr("Выберите раздел управления.", "Choose a management section.")
        )
    if screen == "bots":
        return (
            _render_entries(tr("🤖 Мои боты", "🤖 My bots"), snapshot.bots)
            + tr("\n\nЗарегистрированные боты ещё не принимают покупателей. "
                 "после настройки каналов, тарифов и способов оплаты и проверки запуска.",
                 "\n\nRegistered bots do not accept buyers until channels, "
                 "plans, payment methods and runtime are configured.")
        )
    if screen == "resources":
        return _render_entries(
            tr("📢 Каналы / группы", "📢 Channels / groups"), snapshot.resources,
        )
    if screen == "plans":
        return _render_entries(tr("📦 Тарифы", "📦 Plans"), snapshot.plans)
    if screen == "methods":
        return _render_entries(
            tr("💳 Способы оплаты", "💳 Payment methods"), snapshot.payment_methods,
        )
    if screen == "settings":
        return (
            tr("⚙️ <b>Настройки проекта</b>\n\n",
               "⚙️ <b>Project settings</b>\n\n")
            + f"📁 {escape(snapshot.project_name)}\n"
            + tr("🌍 Текущий часовой пояс: ", "🌍 Current timezone: ")
            + f"<b>{escape(snapshot.timezone)}</b>\n\n"
            + tr("Время в базе хранится в UTC. Даты подписок отображаются "
                 "с учётом часового пояса проекта.",
                 "Database timestamps use UTC. Subscription dates are "
                 "displayed in the project's timezone.")
        )
    if screen == "timezone":
        return (
            tr("🌍 <b>Часовой пояс проекта</b>\n\n",
               "🌍 <b>Project timezone</b>\n\n")
            + tr("Сейчас: ", "Current: ")
            + f"<b>{escape(snapshot.timezone)}</b>\n\n"
            + tr("Выберите зону ниже или отправьте команду "
                 "<code>/timezone Region/City</code> для любой IANA-зоны.\n"
                 "По умолчанию для новых проектов — Europe/Moscow.",
                 "Choose a timezone below or send "
                 "<code>/timezone Region/City</code> for any IANA timezone.\n"
                 "Default for new projects: Europe/Moscow.")
        )
    raise ValueError("Unknown admin screen")

def _private_chat(message: Message) -> bool:
    return message.chat.type == "private" and message.from_user is not None


class BotTokenReplyFilter(BaseFilter):
    async def __call__(self, message: Message, bot: Bot) -> bool:
        if not _private_chat(message) or message.reply_to_message is None:
            return False
        prompt = message.reply_to_message
        return bool(
            prompt.from_user is not None
            and prompt.from_user.id == bot.id
            and (prompt.text or "").startswith(
                (CONNECT_PROMPT_PREFIX, CONNECT_PROMPT_PREFIX_EN)
            )
        )


class StrayBotTokenFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(
            _private_chat(message)
            and message.text is not None
            and TOKEN_PATTERN.fullmatch(message.text.strip())
        )


def create_telegram_admin_router(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
) -> Router:
    router = Router(name="telegram-admin")

    async def show_screen(
        bot: Bot,
        telegram_user_id: int,
        screen: str,
        *,
        edit_message_id: int | None = None,
    ) -> None:
        snapshot = await load_admin_project(
            session_factory,
            project_id=project_id,
            actor_telegram_user_id=telegram_user_id,
        )
        text = _screen_text(screen, snapshot)
        keyboard = _keyboard(screen, snapshot)
        if edit_message_id is not None:
            try:
                await bot.edit_message_text(
                    chat_id=telegram_user_id,
                    message_id=edit_message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                return
            except AiogramError:
                pass
        await bot.send_message(
            chat_id=telegram_user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    @router.message(Command("start", "menu", "admin"))
    async def admin_home_handler(message: Message, bot: Bot) -> None:
        # This is the central owner-management bot, not a buyer storefront.
        # Keep /admin as a backwards-compatible alias, but don't require it.
        if not _private_chat(message):
            return
        command = (message.text or "").split(maxsplit=1)[0].split("@", 1)[0]
        screen = "bots" if command == "/start" else "main"
        try:
            await show_screen(bot, message.from_user.id, screen)
        except PermissionError:
            if command == "/admin":
                text = tr("⛔ Нет доступа к управлению проектом.",
                          "⛔ Project management access denied.")
            else:
                text = (
                    tr(
                        "👋 <b>ViMi Pay · Управление ботами</b>\n\n"
                        "Это центральный бот для владельцев каналов, "
                        "а не бот покупки подписок.\n\n"
                        "У вас пока нет доступа к текущему проекту. "
                        "Самостоятельное создание новых проектов "
                        "будет добавлено в следующем этапе.",
                        "👋 <b>ViMi Pay · Bot management</b>\n\n"
                        "This central bot is for channel owners, "
                        "not for purchasing subscriptions.\n\n"
                        "You do not have access to the selected project. "
                        "Please use the central connection flow to manage your bots.",
                    )
                )
            await bot.send_message(
                chat_id=message.from_user.id,
                text=text,
                parse_mode="HTML",
            )

    @router.message(Command("timezone"))
    async def timezone_command_handler(message: Message, bot: Bot) -> None:
        if not _private_chat(message):
            return
        _, _, name = (message.text or "").partition(" ")
        name = name.strip()
        try:
            if name:
                await change_project_timezone(
                    session_factory,
                    project_id=project_id,
                    actor_telegram_user_id=message.from_user.id,
                    timezone_name=name,
                )
            await show_screen(bot, message.from_user.id, "timezone")
        except PermissionError:
            await bot.send_message(
                chat_id=message.from_user.id,
                text=tr("⛔ Нет доступа к управлению проектом.",
                        "⛔ Project management access denied."),
            )
        except ValueError as exc:
            await bot.send_message(chat_id=message.from_user.id, text=str(exc))

    @router.callback_query(F.data.startswith("adm:"))
    async def admin_navigation_handler(callback: CallbackQuery, bot: Bot) -> None:
        if callback.data is None or callback.message is None:
            return
        if callback.message.chat.type != "private":
            await callback.answer(
                tr("Используйте личный чат с ботом", "Use a private chat with the bot"),
                show_alert=True,
            )
            return
        action = callback.data[4:]
        user_id = callback.from_user.id
        message_id = callback.message.message_id

        try:
            if action == "connect":
                await require_bot_owner(
                    session_factory,
                    project_id=project_id,
                    actor_telegram_user_id=user_id,
                )
                if get_settings().bot_token_encryption_key is None:
                    await callback.answer(
                        tr("На сервере ещё не настроено шифрование токенов",
                           "Bot token encryption is not configured."),
                        show_alert=True,
                    )
                    return
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        tr(
                            f"{CONNECT_PROMPT_PREFIX}\n\n"
                            "1. Создайте бота через @BotFather.\n"
                            "2. Ответьте на ЭТО сообщение полученным токеном.\n"
                            "3. ViMi Pay проверит бота и удалит сообщение с токеном.\n\n"
                            "Важно: бот будет зарегистрирован, но запустится "
                            "для покупателей после настройки и запуска продаж.",
                            f"{CONNECT_PROMPT_PREFIX_EN}\n\n"
                            "1. Create a bot using @BotFather.\n"
                            "2. Reply to THIS message with its token.\n"
                            "3. ViMi Pay verifies your bot and deletes the token message.\n\n"
                            "Your bot is registered first; buyers can use it "
                            "only after setup and launch.",
                        )
                    ),
                    reply_markup=ForceReply(
                        selective=True,
                        input_field_placeholder=tr("Токен BotFather", "BotFather token"),
                    ),
                )
                await callback.answer()
                return

            if action.startswith("tz:"):
                await change_project_timezone(
                    session_factory,
                    project_id=project_id,
                    actor_telegram_user_id=user_id,
                    timezone_name=action[3:],
                )
                action = "timezone"

            if action in SCREENS:
                await show_screen(bot, user_id, action, edit_message_id=message_id)
            elif action == "client":
                # During staged migration the central bot is either the old
                # combined runtime or management-only. Do not sell through
                # the central bot once customer traffic is separated.
                snapshot = await load_admin_project(
                    session_factory, project_id=project_id, actor_telegram_user_id=user_id
                )
                if get_settings().telegram_central_access_enabled:
                    await _send_catalog(
                        bot,
                        chat_id=user_id,
                        session_factory=session_factory,
                        project_id=project_id,
                    )
                elif len(snapshot.bots) == 1:
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            tr("👁 Покупательский каталог открыт в вашем боте:\n",
                               "👁 Buyer catalog is available in your bot:\n")
                            + f"@{escape(snapshot.bots[0].name.lstrip('@'))}\n\n"
                            + tr("Откройте его через Telegram и отправьте /start.",
                                 "Open it in Telegram and send /start.")
                        ),
                    )
                else:
                    await bot.send_message(
                        chat_id=user_id,
                        text=tr("Сначала выберите и запустите собственного бота.",
                                "Select and launch your connected bot first."),
                    )
            elif action == "reviews":
                snapshot = await load_admin_project(
                    session_factory, project_id=project_id, actor_telegram_user_id=user_id
                )
                if not get_settings().telegram_central_access_enabled:
                    if len(snapshot.bots) == 1:
                        target = escape(snapshot.bots[0].name)
                        info = (
                            tr("🧾 Заявки теперь обрабатываются в вашем боте ",
                               "🧾 Payments are reviewed inside your bot ")
                            + f"{target}. "
                            + tr("Откройте его и отправьте /payments.\n\n"
                                 "Старые кнопки подтверждения в ViMi Pay больше не работают.",
                                 "Open it and send /payments.\n\n"
                                 "Old central-bot approval controls no longer work.")
                        )
                    else:
                        info = (
                            tr("🧾 Для обработки заявок откройте своего "
                               "подключённого бота и отправьте /payments.",
                               "🧾 Open your connected bot and send "
                               "/payments to review payment requests.")
                        )
                    await bot.send_message(chat_id=user_id, text=info)
                else:
                    async with system_project_session_scope(session_factory, project_id) as session:
                        pending = await list_pending_reviews_for_actor(
                            session,
                            project_id=project_id,
                            actor_telegram_user_id=user_id,
                            actor_username=callback.from_user.username,
                            actor_first_name=callback.from_user.first_name,
                        )
                    if not pending:
                        await bot.send_message(
                            chat_id=user_id,
                            text=tr("🧾 Заявок на проверку сейчас нет.",
                                    "🧾 No payment requests awaiting review."),
                        )
                    for item in pending:
                        await bot.send_message(
                            chat_id=user_id,
                            text=_review_text(item),
                            reply_markup=_review_keyboard(item.request_id),
                        )
            else:
                await callback.answer(tr("Неизвестное действие", "Unknown action"),
                                      show_alert=True)
                return
        except PermissionError:
            await callback.answer(tr("Нет доступа к управлению", "Management access denied"),
                                  show_alert=True)
            return
        except ValueError as exc:
            await callback.answer(
                tr(str(exc)[:180], "Invalid request or timezone. Please retry."),
                show_alert=True,
            )
            return

        await callback.answer()

    @router.message(BotTokenReplyFilter())
    async def connect_bot_token_handler(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        token = message.text or ""
        # Telegram private messages are not end-to-end encrypted. Delete first,
        # fail closed if Telegram does not confirm deletion; never echo the token.
        try:
            deleted = await bot.delete_message(
                chat_id=message.chat.id, message_id=message.message_id
            )
        except AiogramError:
            deleted = False
        if not deleted:
            await bot.send_message(
                chat_id=message.chat.id,
                text=tr("Не удалось удалить сообщение с токеном. Подключение отменено.",
                        "Could not delete the token message. Connection canceled."),,
            )
            return

        try:
            result = await register_customer_bot(
                session_factory,
                project_id=project_id,
                actor_telegram_user_id=message.from_user.id,
                token=token.strip(),
                encryption_key=get_settings().bot_token_encryption_key,
                central_bot_id=bot.id,
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            await bot.send_message(
                chat_id=message.chat.id,
                text=tr(str(exc), "Bot connection failed. Check the token and retry."),
            )
            return

        await bot.send_message(
            chat_id=message.chat.id,
            text=(
                tr("✅ Бот ", "✅ Bot ")
                + f"<b>@{escape(result.username)}</b> "
                + tr("зарегистрирован.\n\n"
                     "Теперь настройте канал, тарифы и оплату. После этого "
                     "проверим готовность вашего бота к продажам.\n"
                     "Откройте /start для просмотра своих ботов.",
                     "registered.\n\n"
                     "Set up your channel, plans and payments first. "
                     "Your bot can start selling after readiness checks.\n"
                     "Open /start to view your bots.")
            ),
            parse_mode="HTML",
        )

    @router.message(StrayBotTokenFilter())
    async def stray_bot_token_handler(message: Message, bot: Bot) -> None:
        # A token sent outside the expected ForceReply is never accepted.
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
        except AiogramError:
            await bot.send_message(
                chat_id=message.chat.id,
                text=tr("Удалите сообщение с токеном вручную и смените токен в BotFather.",
                        "Delete the token message manually and rotate it in BotFather."),,
            )
            return
        await bot.send_message(
            chat_id=message.chat.id,
            text=tr("Для подключения используйте /start → Подключить своего бота.",
                    "Use /start → Connect my bot to connect a token."),,
        )

    return router
