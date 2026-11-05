"""Owner bot administration with a restricted central connection registrar."""

from __future__ import annotations

from html import escape
from secrets import randbelow
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.db import (
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.localization import current_language, recipient_language, tr
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.deletion_service import (
    request_bot_retirement,
)
from telegram_membership_saas.modules.bots.service import (
    TOKEN_PATTERN,
    decrypt_token,
    register_customer_bot,
)
from telegram_membership_saas.modules.owner_setup.buyer_preview import (
    preview_catalog_screen,
    preview_method_screen,
    preview_offer_screen,
)
from telegram_membership_saas.modules.owner_setup.connection_management import (
    archive_owner_connection,
    get_connection_disposition,
    list_archived_owner_connections,
    remove_unused_connection,
    rename_owner_connection,
    restore_owner_connection,
)
from telegram_membership_saas.modules.owner_setup.error_locale import owner_error
from telegram_membership_saas.modules.owner_setup.labels import (
    owner_button_label,
    owner_runtime_label,
)
from telegram_membership_saas.modules.owner_setup.method_management import (
    change_owner_method_status_guarded,
    delete_unused_owner_method,
    detach_owner_method_currency,
    edit_owner_method_instructions,
    rename_owner_method,
)
from telegram_membership_saas.modules.owner_setup.network_profiles import (
    get_project_import_status,
    read_owner_network_profile,
    save_owner_network_profile,
    set_network_auto_apply,
)
from telegram_membership_saas.modules.owner_setup.obligation_locale import (
    obligation_error,
    obligation_message,
    obligation_status,
)
from telegram_membership_saas.modules.owner_setup.obligations import (
    close_owner_payment,
    end_owner_subscription,
    list_owner_obligations,
    retry_owner_access,
    revoke_owner_grant,
)
from telegram_membership_saas.modules.owner_setup.plan_management import (
    archive_owner_plan,
    delete_unused_owner_plan,
    edit_owner_draft_duration,
    edit_owner_draft_resource,
    rename_owner_plan,
)
from telegram_membership_saas.modules.owner_setup.service import (
    ALLOWED_CURRENCIES,
    SetupFlow,
    advance_flow,
    back_owner_flow,
    bind_prompt,
    can_manage_project,
    cancel_owner_flow,
    choose_project,
    consume_button_flow,
    consume_flow,
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    ensure_telegram_actor,
    get_selected_project,
    is_expected_prompt,
    owner_available_currencies,
    owner_home,
    publish_owner_plan,
    register_new_owner_bot,
    save_owner_price,
    set_default_project_name_from_bot,
    set_flow,
    setup_overview,
    verify_channel_and_add,
)
from telegram_membership_saas.modules.owner_setup.tenant_admin import (
    METHOD_PAGE_SIZE,
    attach_owner_method_currency,
    get_owner_method_info,
    get_owner_plan_info,
    list_owner_methods,
)
from telegram_membership_saas.modules.payments.models import PaymentMethod
from telegram_membership_saas.modules.telegram_admin.service import change_project_timezone
from telegram_membership_saas.modules.telegram_commerce.handlers import (
    _review_keyboard,
    _review_text,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    decode_uuid,
    encode_uuid,
    format_amount,
    format_period,
    list_pending_reviews_for_actor,
)
from telegram_membership_saas.modules.telegram_commerce.storefront import list_buyer_offers
from telegram_membership_saas.telegram_menus import set_chat_commands
from telegram_membership_saas.tenant_context import TenantContext

DURATIONS = {
    "week": (1, "week", "7 дней"),
    "month": (1, "month", "1 месяц"),
    "quarter": (3, "month", "3 месяца"),
    "half": (6, "month", "6 месяцев"),
    "year": (1, "year", "1 год"),
    "lifetime": (None, "lifetime", "Навсегда"),
}


def _button(label: str, data: str, style: str = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=owner_button_label(label), callback_data="own:" + data, style=style,
    )


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _status(value: str) -> str:
    return {
        "active": tr("🟢 Активно", "🟢 Active"),
        "registered": tr("✅ Подключён", "✅ Connected"),
        "draft": tr("📝 Черновик", "📝 Draft"),
        "disabled": tr("⚪ Отключено", "⚪ Disabled"),
        "archived": tr("📦 Архив", "📦 Archived"),
        "deleting": tr("🟡 Удаляется", "🟡 Removing"),
    }.get(value, tr("Неизвестно", "Unknown"))


def _next_setup_step(snapshot) -> tuple[str, str] | None:
    """One contextual action at a time, not an error-driven wizard."""
    if not any(status == "active" for _, _, status in snapshot.resources):
        return tr("📢 1. Подключить канал", "📢 1. Connect a channel"), "resources"
    if not any(status == "active" for _, _, status in snapshot.methods):
        return tr("💳 2. Настроить оплату", "💳 2. Set up payments"), "methods"
    if not any(status == "active" for _, _, status in snapshot.plans):
        return tr("📦 3. Подготовить тариф", "📦 3. Create a plan"), "plans"
    if not snapshot.sales_configured:
        # A published plan can lose its last currency-compatible payment
        # method without losing its draft prices. Readiness must lead to an
        # actionable repair, never back to itself.
        return tr("💳 4. Проверить валюты оплаты", "💳 4. Check payment currencies"), "methods"
    return None


def _private(message: Message) -> bool:
    return message.chat.type == "private" and message.from_user is not None


def _progress(snapshot) -> tuple[str, ...]:
    # A registered bot is configured, not necessarily running.
    steps = (
        (tr("Собственный бот", "Connected bot"), any(s == "registered" for _, s in snapshot.bots)),
        (tr("Канал / группа", "Channel / group"),
         any(s == "active" for _, _, s in snapshot.resources)),
        (tr("Тариф", "Plan"), any(s == "active" for _, _, s in snapshot.plans)),
        (tr("Способ оплаты", "Payment method"), any(s == "active" for _, _, s in snapshot.methods)),
        (tr("Бот принимает покупателей", "Bot accepts buyers"), bool(snapshot.live_bot_username)),
    )
    return tuple(("✅ " if ready else "▫️ ") + label for label, ready in steps)


class StrayBotTokenFilter(BaseFilter):
    """Scrub credentials even when there is no live ForceReply wizard."""

    async def __call__(self, message: Message) -> bool:
        return bool(
            _private(message)
            and message.text is not None
            and TOKEN_PATTERN.fullmatch(message.text.strip())
        )


class SetupReplyFilter(BaseFilter):
    def __init__(
        self, factory: async_sessionmaker[AsyncSession],
        fixed_project_id: UUID | None = None,
    ) -> None:
        self.factory = factory
        self.fixed_project_id = fixed_project_id

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if not _private(message) or message.reply_to_message is None:
            return False
        origin = message.reply_to_message
        if origin.from_user is None or origin.from_user.id != bot.id:
            return False
        user = message.from_user
        actor_id = await ensure_telegram_actor(
            self.factory, telegram_id=user.id,
            username=user.username, first_name=user.first_name,
        )
        if self.fixed_project_id is not None and not await can_manage_project(
            self.factory, actor_id=actor_id, project_id=self.fixed_project_id,
        ):
            return False
        return await is_expected_prompt(
            self.factory, actor_id=actor_id,
            reply_message_id=origin.message_id,
            bot_scope_id=bot.id if self.fixed_project_id else 0,
        )


class OwnerHomeFilter(BaseFilter):
    """Owner / tenant manager sees administration; ordinary buyers fall through."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession],
        fixed_project_id: UUID | None,
    ) -> None:
        self.factory = factory
        self.fixed_project_id = fixed_project_id

    async def __call__(self, message: Message) -> bool:
        if not _private(message):
            return False
        if self.fixed_project_id is None:
            return True
        user = message.from_user
        actor_id = await ensure_telegram_actor(
            self.factory, telegram_id=user.id,
            username=user.username, first_name=user.first_name,
        )
        return await can_manage_project(
            self.factory, actor_id=actor_id, project_id=self.fixed_project_id,
        )


def create_owner_setup_router(
    factory: async_sessionmaker[AsyncSession], *,
    fixed_project_id: UUID | None = None,
) -> Router:
    router = Router(name="owner-tenant" if fixed_project_id else "central-registrar")

    async def actor(user) -> UUID:
        return await ensure_telegram_actor(
            factory, telegram_id=user.id,
            username=user.username, first_name=user.first_name,
        )

    def platform_navigation(user_id: int) -> list[InlineKeyboardButton]:
        """Switch roles only on central and only for configured owner."""
        if (
            fixed_project_id is None
            and getattr(get_settings(), "platform_owner_telegram_id", None) == user_id
        ):
            return [InlineKeyboardButton(
                text=tr("👑 Панель платформы", "👑 Platform dashboard"), callback_data="plat:home",
            )]
        return []

    async def show_home(bot: Bot, user_id: int, actor_id: UUID) -> None:
        home = await owner_home(
            factory, actor_id=actor_id,
            bot_scope_id=bot.id if fixed_project_id else 0,
            fixed_project_id=fixed_project_id,
        )
        if home.selected is not None:
            await show_dashboard(bot, user_id, actor_id, home.selected)
            return
        if fixed_project_id is None and (home.projects or home.archived_count):
            # Even with zero active projects, keep archived connections
            # reachable through /start, /menu and /bots.
            await show_connections(bot, user_id, actor_id)
            return
        lines = [tr("✨ <b>ViMi Pay · Управление подписками</b>",
                    "✨ <b>ViMi Pay · Membership management</b>"), ""]
        rows: list[list[InlineKeyboardButton]] = []
        if not home.projects:
            lines.append(
                tr("Подключите собственного Telegram-бота, затем "
                   "откройте его для настройки тарифов и оплаты.",
                   "Connect your Telegram bot, then open it to configure "
                   "subscriptions and payments.")
            )
        else:
            lines.append(tr("Выберите проект для управления:", "Select a connection to manage:"))
            for project_id, name in home.projects[:20]:
                rows.append([_button("🤖 " + name[:45], "select:" + project_id.hex)])
        rows.append([_button("➕ Подключить бота подписок", "new", "success")])
        rows.append([InlineKeyboardButton(text="🌐 Language / Язык", callback_data="lng:menu")])
        if platform_navigation(user_id):
            rows.append(platform_navigation(user_id))
        await bot.send_message(
            user_id, "\n".join(lines),
            parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_dashboard(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        data = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        if fixed_project_id is None:
            # Central is a connection manager, not a tenant sales console.
            text = (
                tr("✦ <b>ViMi Pay</b>\n<i>Ваши подключения</i>\n\n",
                   "✦ <b>ViMi Pay</b>\n<i>Your connections</i>\n\n") +
                f"🤖 <b>{escape(data.project_name)}</b>\n"
                f"{escape(owner_runtime_label(data.bot_runtime_label))}\n\n"
                + tr("Подключите бота и продолжите настройку внутри него.",
                     "Connect a bot and finish setup inside that bot.")
            )
            rows = [
                [_button("🤖 Управление ботом", "bots", "success")],
                [_button("📁 Все подключения", "projects")],
                [_button("❔ Как это работает", "help")],
                [InlineKeyboardButton(text="🌐 Language / Язык", callback_data="lng:menu")],
            ]
            if platform_navigation(user_id):
                rows.append(platform_navigation(user_id))
        else:
            next_step = _next_setup_step(data)
            text = (
                tr("✦ <b>Панель администратора</b>\n",
                   "✦ <b>Admin dashboard</b>\n") +
                f"<b>{escape(data.project_name)}</b>\n"
                f"🌍 {escape(data.timezone)}\n\n"
                + "\n".join(_progress(data))
                + "\n\n"
                + (
                    tr("✅ Продажи готовы. Управляйте тарифами и заявками.",
                        "✅ Ready for sales. Manage plans and payment requests.")
                    if data.sales_configured
                    else tr("Настройте продажи по порядку. Уже сохранённые "
                            "данные останутся на месте.",
                            "Complete the setup steps. Saved settings will remain intact.")
                )
            )
            rows = []
            if next_step is not None:
                label, action = next_step
                rows.append([_button(label, action, "success")])
            rows.extend([
                [_button("📢 Каналы", "resources"),
                 _button("💳 Оплата", "methods")],
                [_button("📦 Тарифы", "plans"),
                 _button("📋 Заявки", "reviews")],
                [_button("📋 Готовность", "readiness"),
                 _button("🌍 TimeZone", "timezone")],
                [_button(tr("📁 Профиль сети", "📁 Network profile"), "network")],
                [_button("👀 Смотреть гостем", "preview")],
                [_button("❔ Помощь", "help")],
                [InlineKeyboardButton(text="🌐 Language / Язык", callback_data="lng:menu")],
            ])
        await bot.send_message(
            user_id, text, parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_network_profile(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        # Profiles are owned by the actor, not by project administrators.
        profile = await read_owner_network_profile(factory, actor_id=actor_id)
        if profile is None:
            lines = [tr(
                "📁 <b>Профиль сети</b>\n\n"
                "Сохраните тарифы, цены и способы оплаты одного бота. "
                "Новые подключённые боты получат независимые черновики "
                "и отключённую оплату.",
                "📁 <b>Network profile</b>\n\n"
                "Save one bot's plans, prices and payment methods. "
                "Future connected bots receive independent drafts "
                "and disabled payment methods.",
            )]
            buttons = [[_button(
                tr("📋 Сохранить настройки", "📋 Save settings"),
                "networkask", "success",
            )]]
        else:
            lines = [
                tr("📁 <b>Профиль сети</b>", "📁 <b>Network profile</b>"),
                tr("Источник: ", "Source: ") + escape(profile.source),
                tr("Версия: ", "Version: ") + str(profile.version),
                tr("Тарифы: ", "Plans: ") + str(profile.plan_count),
                tr("Способы оплаты: ", "Payment methods: ")
                + str(profile.method_count),
                (tr("Реквизиты: копируются", "Instructions: copied")
                 if profile.copy_instructions else tr(
                    "Реквизиты: заполняются отдельно",
                    "Instructions: set separately",
                )),
                tr("Автоприменение: ", "Auto-apply: ") + (
                    tr("✅ Включено", "✅ Enabled") if profile.enabled
                    else tr("⏸️ Отключено", "⏸️ Disabled")
                ),
                "",
                tr(
                    "Изменения профиля затронут только будущие подключения. "
                    "Существующие боты и подписчики не меняются.",
                    "Profile changes affect only future connections. "
                    "Existing bots and subscribers remain unchanged.",
                ),
            ]
            buttons = [[_button(
                tr("🔄 Обновить из этого бота", "🔄 Update from this bot"),
                "networkask",
            )]]
            buttons.append([_button(
                tr("⏸️ Отключить автокопирование", "⏸️ Pause auto-copy")
                if profile.enabled else tr(
                    "▶️ Включить автокопирование", "▶️ Resume auto-copy",
                ),
                "networkoff" if profile.enabled else "networkon",
            )])
        buttons.append([_button(
            tr("⬅️ Панель администратора", "⬅️ Admin dashboard"),
            "dashboard",
        )])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(buttons),
        )


    async def prompt(
        bot: Bot, user_id: int, actor_id: UUID,
        *, step: str, text: str, payload: dict | None = None,
        source_flow: SetupFlow | None = None,
    ) -> None:
        if source_flow is None:
            generation = await set_flow(
                factory, actor_id=actor_id, step=step, payload=payload,
                project_id=fixed_project_id,
                bot_scope_id=bot.id if fixed_project_id else 0,
            )
        else:
            generation = await advance_flow(
                factory, actor_id=actor_id, source=source_flow,
                step=step, payload=payload or {},
                bot_scope_id=bot.id if fixed_project_id else 0,
            )
        sent = await bot.send_message(
            user_id,
            text + tr(
                "\n\n↩ /back — назад · /cancel — в меню.",
                "\n\n↩ /back — back · /cancel — main menu.",
            ),
            parse_mode="HTML",
            reply_markup=ForceReply(
                selective=True, input_field_placeholder=tr("Введите ответ", "Type your reply")
            ),
        )
        await bind_prompt(
            factory, actor_id=actor_id, step=step,
            message_id=sent.message_id, generation=generation,
            bot_scope_id=bot.id if fixed_project_id else 0,
        )

    async def show_connections(
        bot: Bot, user_id: int, actor_id: UUID,
        *, page: int = 0, archived: bool = False,
    ) -> None:
        """Compact, paginated central connection registry (100+ bots)."""
        if archived:
            projects = await list_archived_owner_connections(
                factory, actor_id=actor_id,
            )
        else:
            home = await owner_home(factory, actor_id=actor_id)
            projects = home.projects
        page_size = 10
        total_pages = max(1, (len(projects) + page_size - 1) // page_size)
        page = min(max(page, 0), total_pages - 1)
        listed = projects[page * page_size:(page + 1) * page_size]
        rows: list[list[InlineKeyboardButton]] = [
            [_button(
                ("📦 " if archived else "📁 ") + name[:42],
                ("archview:" if archived else "select:") + pid.hex,
            )]
            for pid, name in listed
        ]
        if total_pages > 1:
            nav = []
            verb = "archives:" if archived else "projects:"
            if page > 0:
                nav.append(_button("◀️", verb + str(page - 1)))
            nav.append(_button(f"{page + 1}/{total_pages}", verb + str(page)))
            if page + 1 < total_pages:
                nav.append(_button("▶️", verb + str(page + 1)))
            rows.append(nav)
        if archived:
            rows.append([_button(
                tr("⬅️ Мои подключения", "⬅️ My connections"), "projects",
            )])
        else:
            rows.append([_button(
                tr("➕ Подключить бота", "➕ Connect a bot"), "new", "success",
            )])
            if await list_archived_owner_connections(factory, actor_id=actor_id):
                rows.append([_button(
                    tr("📦 Архив подключений", "📦 Archived connections"),
                    "archives",
                )])
        if platform_navigation(user_id):
            rows.append(platform_navigation(user_id))
        title = tr("📁 <b>Мои подключения</b>", "📁 <b>My connections</b>")
        if archived:
            title = tr("📦 <b>Архив подключений</b>", "📦 <b>Archived connections</b>")
        await bot.send_message(
            user_id,
            title + "\n" + (
                tr("Пусто.", "Empty.") if not projects
                else tr(
                    "Выберите подключение для управления.",
                    "Select a connection to manage.",
                )
            ),
            parse_mode="HTML", reply_markup=_markup(rows),
        )


    async def show_archived_connection(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        archived = dict(await list_archived_owner_connections(
            factory, actor_id=actor_id,
        ))
        name = archived.get(project_id)
        if name is None:
            raise ValueError(tr(
                "Подключение не найдено в архиве.", "Connection is not archived.",
            ))
        await bot.send_message(
            user_id,
            tr("📦 <b>В архиве:</b> ", "📦 <b>Archived:</b> ")
            + escape(name) + "\n\n"
            + tr(
                "Бот отключён, финансовая история сохранена. "
                "Подключение не отображается в активном списке.",
                "Bot detached, financial records retained. "
                "This connection is hidden from the active list.",
            ),
            parse_mode="HTML",
            reply_markup=_markup([
                [_button(
                    tr("↩️ Восстановить подключение", "↩️ Restore connection"),
                    "restore:" + project_id.hex, "success",
                )],
                [_button(tr("⬅️ Архив", "⬅️ Archive"), "archives")],
            ]),
        )


    async def show_bots(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        """One connection card, with actions based on its actual state."""
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        lines = [
            tr("✦ <b>Управление подключением</b>",
               "✦ <b>Manage connection</b>"),
            f"<b>{escape(data.project_name)}</b>",
            "",
        ]
        if not data.bots:
            lines.append(tr("⚪ Telegram-бот не подключён.",
                            "⚪ No Telegram bot connected."))
        else:
            for username, status in data.bots:
                lines.append(
                    f"🤖 <b>@{escape(username)}</b> — "
                    + (tr("подключён", "connected") if status == "registered"
                       else _status(status))
                )
        retiring = any(status == "deleting" for _, _, status in data.bot_instances)
        if retiring:
            lines.append(tr(
                "\n⏳ Бот отключается. Обновите статус после завершения.",
                "\n⏳ Bot is being disconnected. Refresh its status afterward.",
            ))
        else:
            lines.append("\n" + escape(owner_runtime_label(data.bot_runtime_label)))
        rows: list[list[InlineKeyboardButton]] = []
        connected = next(
            (username for _, username, status in data.bot_instances
             if status == "registered"), None,
        )
        if connected:
            rows.append([InlineKeyboardButton(
                text=tr("🚀 Открыть бота", "🚀 Open bot"),
                url="https://t.me/" + connected, style="success",
            )])
        if data.bot_instances:
            rows.append([_button(tr("🔄 Обновить статус", "🔄 Refresh"), "bots")])
        if not retiring and data.can_delete_bot:
            rows.append([_button(
                tr("🔑 Подключить / обновить токен", "🔑 Connect / update token"),
                "connect",
            )])
        if data.can_delete_bot:
            rows.append([_button(
                tr("✏️ Переименовать подключение", "✏️ Rename connection"),
                "connrename:" + project_id.hex,
            )])
            if data.bot_instances:
                for instance_id, username, status in data.bot_instances:
                    if status != "deleting":
                        rows.append([_button(
                            tr("🗑 Отключить @", "🗑 Disconnect @") + username[:30],
                            "drop:" + encode_uuid(project_id)
                            + ":" + encode_uuid(instance_id),
                            "danger",
                        )])
            else:
                disposition = await get_connection_disposition(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                if disposition.has_history:
                    rows.append([_button(
                        tr("📦 Убрать в архив", "📦 Archive connection"),
                        "connarchive:" + project_id.hex, "danger",
                    )])
                else:
                    rows.append([_button(
                        tr("🗑 Удалить подключение", "🗑 Delete connection"),
                        "connremove:" + project_id.hex, "danger",
                    )])
        rows.append([_button(
            tr("⬅️ Все подключения", "⬅️ All connections"), "projects",
        )])
        if platform_navigation(user_id):
            rows.append(platform_navigation(user_id))
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(rows),
        )


    async def show_obligations(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        page: int = 0,
    ) -> None:
        snapshot = await list_owner_obligations(
            factory, actor_id=actor_id, project_id=project_id, page=page,
        )
        counts = snapshot.blockers
        lines = [
            tr("🧾 <b>Обязательства перед отключением</b>",
               "🧾 <b>Outstanding obligations before removal</b>"),
            tr("Страница ", "Page ") + str(page + 1)
            + tr(" · до 5 записей каждого типа", " · up to 5 records per category"),
            "",
            tr("Заявки: ", "Payment requests: ") + str(counts.requests)
            + tr(" · Подписки: ", " · Subscriptions: ") + str(counts.subscriptions)
            + tr(" · Права: ", " · Grants: ") + str(counts.grants),
            tr("Доступы: ", "Access states: ") + str(counts.access)
            + tr(" · Действия: ", " · Actions: ") + str(counts.access_actions)
            + tr(" · Приглашения: ", " · Invites: ") + str(counts.join_tickets),
            "",
            tr(
                "Все действия выполняются отдельно с подтверждением. "
                "Оплаченный доступ нельзя отключать без урегулирования "
                "обязательств перед покупателем.",
                "Each action requires individual confirmation. "
                "Do not remove paid access without resolving your "
                "obligations to customers.",
            ),
        ]
        rows: list[list[InlineKeyboardButton]] = []
        project_ref = encode_uuid(project_id)
        for item in snapshot.payments:
            label = (
                tr("Отклонить", "Reject")
                if item.status == "waiting_review"
                else tr("Закрыть", "Close")
            )
            lines.append(
                "💳 " + escape(item.username or str(item.telegram_user_id))
                + " · " + escape(item.amount + " " + item.currency)
                + " · " + escape(obligation_status(item.status))
            )
            rows.append([_button(
                "💳 " + label + tr(" заявку ", " request ")
                + str(item.telegram_user_id),
                "ob:pay:" + project_ref + ":" + encode_uuid(item.id)
                + (":" + str(page) if page else ""), "danger",
            )])
        for item in snapshot.subscriptions:
            end = (
                item.period_end.strftime("%d.%m.%Y")
                if item.period_end else tr("бессрочно", "lifetime")
            )
            lines.append(
                "📦 " + escape(item.plan_name) + " · "
                + escape(item.username or str(item.telegram_user_id))
                + tr(" · до ", " · until ") + end
            )
            rows.append([_button(
                tr("📦 Завершить подписку ", "📦 End subscription ")
                + str(item.telegram_user_id),
                "ob:sub:" + project_ref + ":" + encode_uuid(item.id)
                + (":" + str(page) if page else ""), "danger",
            )])
        for item in snapshot.grants:
            if item.source == "payment":
                continue
            lines.append(
                "🔑 " + escape(item.source) + " · " + str(item.telegram_user_id)
            )
            rows.append([_button(
                tr("🔑 Отозвать ", "🔑 Revoke ")
                + item.source[:12] + " · " + str(item.telegram_user_id),
                "ob:grant:" + project_ref + ":" + encode_uuid(item.id)
                + (":" + str(page) if page else ""), "danger",
            )])
        if snapshot.access:
            lines.append(tr(
                "🔄 Доступы требуют подтверждения фактического отзыва в Telegram. "
                "Они не очищаются принудительно.",
                "🔄 Access revocations must be confirmed in Telegram. "
                "Access states are never forcibly cleared.",
            ))
            for item in snapshot.access[:12]:
                rows.append([_button(
                    tr("🔄 Синхронизировать доступ · ", "🔄 Sync access · ")
                    + obligation_status(item.actual),
                    "ob:sync:" + project_ref + ":" + encode_uuid(item.id)
                    + (":" + str(page) if page else ""),
                )])
        if counts.access_actions or counts.join_tickets:
            lines.append(tr(
                "⏳ Действия доступа и приглашения обрабатывает клиентский бот; "
                "проверьте состояние после завершения.",
                "⏳ The connected bot processes access actions and invitations. "
                "Check the status again when processing completes.",
            ))
        if not counts.has_blockers:
            lines.append(tr(
                "\n✅ Блокировок больше нет. Можно вернуться к удалению.",
                "\n✅ No outstanding blockers. You can return to bot removal.",
            ))
        nav = []
        if page > 0:
            nav.append(_button(
                "⬅️ Назад", "obpage:" + project_ref + ":" + str(page - 1),
            ))
        if page < 200 and any(
            len(items) == 5
            for items in (
                snapshot.payments, snapshot.subscriptions,
                snapshot.grants, snapshot.access,
            )
        ):
            nav.append(_button(
                "➡️ Далее", "obpage:" + project_ref + ":" + str(page + 1),
            ))
        if nav:
            rows.append(nav)
        rows.extend([
            [_button("🔄 Обновить обязательства", "oblist")],
            [_button("⬅️ Мои боты", "bots")],
        ])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(rows),
        )


    async def notify_buyer(
        central_bot: Bot, owner_tg_id: int, actor_id: UUID,
        project_id: UUID, change,
    ) -> None:
        if not change.changed or change.telegram_user_id is None:
            return
        key = get_settings().bot_token_encryption_key
        if not key:
            await central_bot.send_message(
                owner_tg_id, tr("⚠️ Состояние сохранено, но уведомление покупателя недоступно.",
                                "⚠️ Change saved, but buyer notifications are unavailable."),
            )
            return
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            instance = await session.scalar(select(BotInstance).where(
                BotInstance.project_id == project_id,
                BotInstance.status == "registered",
            ).limit(1))
            cipher = instance.token_ciphertext if instance else None
        if not cipher:
            await central_bot.send_message(
                owner_tg_id, tr("⚠️ Изменение сохранено, но бот покупателя отключён.",
                                "⚠️ Change saved, but the buyer bot is disconnected."),
            )
            return
        try:
            customer_bot = Bot(token=decrypt_token(cipher, key=key))
            try:
                async with recipient_language(factory, change.telegram_user_id):
                    await customer_bot.send_message(
                        change.telegram_user_id, obligation_message(change.message),
                    )
            finally:
                await customer_bot.session.close()
        except (AiogramError, ValueError, RuntimeError, InvalidToken):
            await central_bot.send_message(
                owner_tg_id,
                tr("⚠️ Изменение сохранено, но Telegram не доставил уведомление покупателю.",
                   "⚠️ Change saved, but Telegram could not notify the buyer."),
            )


    async def show_resources(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        lines = [tr("📢 <b>Каналы и группы</b>", "📢 <b>Channels and groups</b>"), ""]
        lines.extend(
            f"• {escape(name)} — {_status(status)}"
            for _, name, status in data.resources
        )
        if not data.resources:
            lines.append(tr("Нет подключённых каналов и групп.",
                            "No channels or groups are connected."))
        lines.append(tr(
            "\nСначала добавьте своего бота администратором канала.",
            "\nFirst, add your bot as a channel administrator.",
        ))
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup([
                [_button("➕ Выбрать канал", "pick:channel", "success")],
                [_button("➕ Выбрать группу", "pick:group", "success")],
                [_button("✍️ Ввести ID / @username", "resource:manual")],
                [_button("➡️ Далее: способы оплаты", "methods")],
                [_button("⬅️ Панель администратора", "dashboard")],
            ]),
        )


    async def show_plans(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        rows: list[list[InlineKeyboardButton]] = []
        lines = [tr("📦 <b>Тарифы</b>", "📦 <b>Plans</b>"), ""]
        if not data.plans:
            lines.append(tr("Тарифов пока нет.", "There are no plans yet."))
        for pid, name, status in data.plans[:15]:
            lines.append(f"• <b>{escape(name)}</b> — {_status(status)}")
            rows.append([_button("⚙️ " + name[:45], "plan:" + pid.hex)])
        rows.append([_button("➕ Создать тариф", "newplan", "success")])
        if not data.methods:
            lines.append(tr(
                "\n💳 Настройте оплату, затем публикуйте тарифы.",
                "\n💳 Configure payments before publishing plans.",
            ))
            rows.append([_button("➕ Настроить оплату", "methods")])
        rows.append([_button("⬅️ Панель администратора", "dashboard")])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML", reply_markup=_markup(rows),
        )


    async def show_plan_card(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID, plan_id: UUID,
    ):
        data = await get_owner_plan_info(
            factory, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        rows = []
        codes = await owner_available_currencies(factory, actor_id=actor_id)
        lines = [
            "📦 <b>" + escape(data.name) + "</b>",
            tr("Статус: ", "Status: ") + _status(data.status),
            "⏳ " + escape(format_period(data.duration_value, data.duration_unit)),
            "📢 " + escape(data.resource_name or tr("Канал не привязан", "No channel assigned")),
            "",
            tr("<b>Цены и оплата</b>", "<b>Prices and payments</b>"),
        ]
        if not data.prices:
            lines.append(tr("Цена ещё не указана.", "No price has been set yet."))
        for price in data.prices:
            lines.append(
                ("✅ " if price.method_count else "⚠️ ")
                + escape(
                    format_amount(price.amount, price.precision)
                    + " " + price.currency
                )
                + (
                    tr(" · оплата настроена", " · payment ready")
                    if price.method_count
                    else tr(" · нужен способ оплаты", " · payment method required")
                )
            )
        rows.extend([
            [_button("💰 " + code, "price:" + plan_id.hex + ":" + code)]
            for code in codes
        ])
        if any(not price.method_count for price in data.prices):
            rows.append([
                _button("➕ Подключить способ оплаты", "newmethod", "success"),
            ])
        token = encode_uuid(plan_id)
        lines.append("")
        lines.append(tr("<b>Управление тарифом</b>", "<b>Manage plan</b>"))
        rows.append([_button(
            tr("✏️ Изменить название", "✏️ Rename plan"),
            "plrename:" + token,
        )])
        if data.status == "draft":
            rows.append([
                _button(
                    tr("⏳ Изменить срок", "⏳ Change duration"),
                    "plduration:" + token,
                ),
                _button(
                    tr("📢 Изменить канал", "📢 Change channel"),
                    "plresource:" + token,
                ),
            ])
            rows.append([
                _button(
                    "✅ Опубликовать тариф",
                    "publish:" + token + ":" + data.revision, "success",
                ),
            ])
        elif data.status == "active":
            rows.append([_button(
                tr("⏸️ Снять с продажи", "⏸️ Stop new sales"),
                "plarchive:" + token,
            )])
        elif data.status == "archived":
            rows.append([_button(
                tr("▶️ Вернуть в продажу", "▶️ Resume sales"),
                "plrestore:" + token, "success",
            )])
        if not getattr(data, "has_history", False):
            rows.append([_button(
                tr("🗑 Удалить тариф", "🗑 Delete plan"),
                "pldelete:" + token, "danger",
            )])
        else:
            lines.append(tr(
                "<i>Удаление недоступно: по тарифу есть история покупок. "
                "Снятие с продажи сохраняет подписки и платежи.</i>",
                "<i>Deletion is unavailable: this plan has purchase history. "
                "Archiving keeps subscriptions and payments intact.</i>",
            ))
        rows.append([_button("⬅️ Все тарифы", "plans")])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(rows),
        )


    async def show_plan_duration_editor(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID, plan_id: UUID,
    ) -> None:
        data = await get_owner_plan_info(
            factory, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        if data.status != "draft":
            raise ValueError("Срок можно изменить только у неопубликованного тарифа")
        token = encode_uuid(plan_id)
        rows = [
            [_button(
                tr(label, format_period(value, unit)),
                "plsetperiod:" + token + ":" + key,
            )]
            for key, (value, unit, label) in DURATIONS.items()
        ]
        rows.append([_button(
            tr("⬅️ К тарифу", "⬅️ Back to plan"), "plan:" + plan_id.hex,
        )])
        await bot.send_message(
            user_id, tr(
                "⏳ <b>Изменить срок черновика</b>\n\n"
                "Выберите новый срок. Существующие цены сохранятся.",
                "⏳ <b>Change draft duration</b>\n\n"
                "Choose the new duration. Existing prices are retained.",
            ), parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_plan_resource_editor(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID, plan_id: UUID,
    ) -> None:
        data = await get_owner_plan_info(
            factory, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        if data.status != "draft":
            raise ValueError("Канал можно изменить только у неопубликованного тарифа")
        snapshot = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        token = encode_uuid(plan_id)
        rows = [
            [_button(
                "📢 " + name[:45], "plsetres:" + token + ":" + encode_uuid(rid),
            )]
            for rid, name, status in snapshot.resources if status == "active"
        ]
        rows.append([_button(
            tr("⬅️ К тарифу", "⬅️ Back to plan"), "plan:" + plan_id.hex,
        )])
        await bot.send_message(
            user_id, tr(
                "📢 <b>Изменить канал черновика</b>\n\n"
                "Выберите канал или группу, доступ к которым даст тариф.",
                "📢 <b>Change draft channel</b>\n\n"
                "Select the channel or group this plan will grant access to.",
            ), parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_methods(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        page: int = 0,
    ) -> None:
        methods = await list_owner_methods(
            factory, actor_id=actor_id, project_id=project_id, page=page,
        )
        lines = [
            tr("💳 <b>Способы оплаты</b>", "💳 <b>Payment methods</b>"),
            tr("Страница ", "Page ") + str(page + 1),
            "",
        ]
        rows = []
        if not methods:
            lines.append(
                tr("Нет способов оплаты на этой странице.",
                   "No payment methods on this page.")
                if page else
                tr("Пока ничего не подключено. Для начала выберите валюту "
                   "и добавьте способ перевода.",
                   "No payment methods yet. Choose a currency "
                   "and add a transfer method to get started.")
            )
        for method in methods:
            lines.append(
                "• <b>" + escape(method.name) + "</b> — "
                + _status(method.status) + "\n  "
                + escape(", ".join(method.currencies) or tr("Без валюты", "No currency"))
            )
            rows.append([
                _button("⚙️ " + method.name[:40], "method:" + method.id.hex),
            ])
        navigation = []
        if page > 0:
            navigation.append(_button(
                "⬅️ Назад", "methodpage:" + str(page - 1),
            ))
        if len(methods) == METHOD_PAGE_SIZE and page < 10000:
            navigation.append(_button(
                "➡️ Далее", "methodpage:" + str(page + 1),
            ))
        if navigation:
            rows.append(navigation)
        rows.append([_button("➕ Добавить способ", "newmethod", "success")])
        if methods or page > 0:
            rows.append([_button("➡️ Перейти к тарифам", "plans")])
        rows.append([_button("⬅️ Панель администратора", "dashboard")])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(rows),
        )


    async def show_method_card(
        bot: Bot, user_id: int, actor_id: UUID,
        project_id: UUID, method_id: UUID,
    ) -> None:
        method = await get_owner_method_info(
            factory, actor_id=actor_id, project_id=project_id,
            method_id=method_id,
        )
        rows = [
            [_button(
                tr("✏️ Изменить название", "✏️ Rename"),
                "methodrename:" + method_id.hex,
            )],
            [_button(
                tr("📝 Изменить реквизиты", "📝 Edit instructions"),
                "methodedit:" + method_id.hex,
            )],
            [_button(
                tr("🌐 Управление валютами", "🌐 Manage currencies"),
                "methodlink:" + method_id.hex,
            )],
        ]
        if method.status == "active":
            rows.append([_button(
                tr("⏸️ Отключить для новых покупок", "⏸️ Disable new sales"),
                "methodconfirmoff:" + method_id.hex, "danger",
            )])
        else:
            rows.append([_button(
                tr("▶️ Включить способ оплаты", "▶️ Enable method"),
                "methodon:" + encode_uuid(method_id) + ":" + method.revision,
                "success",
            )])
        if not method.has_history:
            rows.append([_button(
                tr("🗑 Удалить способ оплаты", "🗑 Delete method"),
                "methoddeleteask:" + method_id.hex, "danger",
            )])
        rows.append([_button(
            tr("⬅️ Все способы оплаты", "⬅️ All payment methods"), "methods",
        )])
        history_note = tr(
            "\nУдаление недоступно: есть история покупок.",
            "\nDeletion unavailable: purchase history exists.",
        ) if method.has_history else ""
        instruction_preview = (
            method.instruction[:350]
            + ("…" if len(method.instruction) > 350 else "")
        )
        await bot.send_message(
            user_id,
            "💳 <b>" + escape(method.name) + "</b>\n"
            + tr("Статус: ", "Status: ") + _status(method.status)
            + tr("\nВалюты: ", "\nCurrencies: ")
            + escape(", ".join(method.currencies) or tr("не выбраны", "none selected"))
            + tr("\n\nИнструкция покупателю:\n", "\n\nBuyer instructions:\n")
            + escape(instruction_preview)
            + history_note,
            parse_mode="HTML",
            reply_markup=_markup(rows),
        )

    async def show_reviews(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        if fixed_project_id is None:
            raise PermissionError("Проверяйте заявки в собственном боте")
        async with system_project_session_scope(factory, project_id) as session:
            items = await list_pending_reviews_for_actor(
                session, project_id=project_id,
                actor_telegram_user_id=user_id, limit=15,
            )
        if not items:
            await bot.send_message(
                user_id,
                tr(
                    "📋 <b>Заявки</b>\n\n"
                    "Сейчас нет платежей, ожидающих проверки.\n"
                    "Новые подтверждения придут сюда вместе с чеком.",
                    "📋 <b>Payment reviews</b>\n\n"
                    "There are no payments awaiting review.\n"
                    "New payment confirmations will appear here with a receipt.",
                ),
                parse_mode="HTML",
                reply_markup=_markup([
                    [_button("⬅️ Панель администратора", "dashboard")],
                ]),
            )
            return
        await bot.send_message(
            user_id,
            tr(
                f"📋 <b>Заявки на проверку — {len(items)}</b>\n\n"
                "Перед подтверждением убедитесь, что деньги "
                "действительно поступили в банк.",
                f"📋 <b>Pending reviews — {len(items)}</b>\n\n"
                "Check your bank balance before approving any payment.",
            ),
            parse_mode="HTML",
        )
        for item in items:
            await bot.send_message(
                user_id, _review_text(item),
                reply_markup=_review_keyboard(item.request_id),
            )
        await bot.send_message(
            user_id,
            tr("Все доступные заявки показаны выше.", "All available requests are shown above."),
            reply_markup=_markup([
                [_button("🔄 Обновить заявки", "reviews")],
                [_button("⬅️ Панель администратора", "dashboard")],
            ]),
        )


    async def show_guest_preview(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        *, currency: str | None = None, page: int = 0,
        price_id: UUID | None = None, method_id: UUID | None = None,
        instruction_page: int = 0, message_id: int | None = None,
    ) -> None:
        """Interactive buyer-like display with no checkout/payment writes."""
        if fixed_project_id is None or project_id != fixed_project_id:
            raise PermissionError("Нет прав на управление этим ботом")
        if not await can_manage_project(
            factory, actor_id=actor_id, project_id=project_id,
        ):
            raise PermissionError("Нет прав на управление этим ботом")

        async with system_project_session_scope(factory, project_id) as session:
            offers = await list_buyer_offers(session, project_id=project_id)
            if method_id is not None and price_id is not None:
                # Method must still be checkout-eligible for this exact price.
                eligible = any(
                    offer.option.plan_price_id == price_id
                    and any(m.payment_method_id == method_id for m in offer.methods)
                    for offer in offers
                )
                if not eligible:
                    raise ValueError("Способ оплаты недоступен в этом проекте")
                method = await session.scalar(
                    select(PaymentMethod).where(
                        PaymentMethod.id == method_id,
                        PaymentMethod.project_id == project_id,
                        PaymentMethod.status == "active",
                    )
                )
                if method is None:
                    raise ValueError("Способ оплаты недоступен в этом проекте")
                rendered = preview_method_screen(
                    offers, price_id=price_id, payment_method_id=method_id,
                    instruction_template=method.instruction_template, page=page,
                    instruction_page=instruction_page,
                )
            elif price_id is not None:
                rendered = preview_offer_screen(
                    offers, price_id=price_id, page=page,
                )
            else:
                rendered = preview_catalog_screen(
                    offers, currency=currency, page=page,
                )
        if rendered is None:
            raise ValueError("Тариф не найден в этом проекте")
        content, keyboard = rendered
        if message_id is not None:
            try:
                await bot.edit_message_text(
                    content, chat_id=user_id, message_id=message_id,
                    parse_mode="HTML", reply_markup=keyboard,
                )
                return
            except AiogramError as exc:
                if "message is not modified" in str(exc).lower():
                    return
                # Older/deleted messages fall back to a new safe preview.
        await bot.send_message(
            user_id, content, parse_mode="HTML", reply_markup=keyboard,
        )


    async def show_help(bot: Bot, user_id: int) -> None:
        rows = (
            [
                [_button("📢 Каналы", "resources")],
                [_button("📦 Тарифы", "plans")],
                [_button("💳 Оплата", "methods")],
                [_button("⬅️ Панель администратора", "dashboard")],
            ]
            if fixed_project_id is not None
            else [
                [_button("🤖 Управление ботом", "bots")],
                [_button("⬅️ Мои подключения", "projects")],
            ]
        )
        await bot.send_message(
            user_id,
            tr(
                "❔ <b>Как запустить подписки</b>\n\n"
                "1. Подключите токен в @vimipay_bot.\n"
                "2. Откройте собственный бот и добавьте его в канал.\n"
                "3. Настройте валюты и способы оплаты.\n"
                "4. Создайте тариф, цену и опубликуйте.\n\n"
                "Ваши чеки ViMi Pay не хранит — поступления "
                "проверяются администратором в банке.",
                "❔ <b>How to launch subscriptions</b>\n\n"
                "1. Connect a bot token in @vimipay_bot.\n"
                "2. Open your bot and add it to your channel.\n"
                "3. Set up currencies and payment methods.\n"
                "4. Create, price and publish a plan.\n\n"
                "ViMi Pay does not retain receipts. "
                "Channel admins verify transfers in their banks.",
            ),
            parse_mode="HTML", reply_markup=_markup(rows),
        )


    async def show_duration_picker(
        bot: Bot, user_id: int, generation: str,
    ) -> None:
        """Provide a safe Back action tied to this exact wizard generation."""
        rows = [
            [_button(
                tr(label, format_period(value, unit)),
                "duration:" + key + ":" + generation,
            )]
            for key, (value, unit, label) in DURATIONS.items()
        ]
        rows.append([_button(
            tr("⬅️ Все тарифы", "⬅️ All plans"),
            "flowback:plans:" + generation,
        )])
        await bot.send_message(
            user_id, tr("⏳ Выберите срок действия:", "⏳ Choose plan duration:"),
            reply_markup=_markup(rows),
        )

    async def currency_picker(
        bot: Bot, user_id: int, actor_id: UUID, *,
        mode: str, plan_id: UUID | None = None,
    ):
        codes = await owner_available_currencies(factory, actor_id=actor_id)
        rows = [
            [_button(
                code, f"methodcurrency:{code}" if mode == "method"
                else f"price:{plan_id.hex}:{code}"
            )]
            for code in codes
        ]
        rows.append([_button(
            tr("⬅️ К способам оплаты", "⬅️ Payment methods"),
            "methods",
        )])
        await bot.send_message(
            user_id, tr("💱 Выберите валюту:", "💱 Choose a currency:"),
            reply_markup=_markup(rows),
        )

    @router.message(
        Command("start", "menu", "bots", "plans", "help", "support"),
        OwnerHomeFilter(factory, fixed_project_id),
    )
    async def home_message(message: Message, bot: Bot) -> None:
        if not _private(message):
            return
        actor_id = await actor(message.from_user)
        command = (
            (getattr(message, "text", None) or "/start")
            .split(maxsplit=1)[0].split("@")[0].lower()
        )
        role = (
            "owner" if fixed_project_id is not None
            else "platform" if message.from_user.id == get_settings().platform_owner_telegram_id
            else "central"
        )
        await set_chat_commands(
            bot, chat_id=message.from_user.id, role=role,
            language=current_language(),
        )
        if command in ("/help", "/support"):
            await show_help(bot, message.chat.id)
        elif command == "/plans" and fixed_project_id is not None:
            await show_plans(bot, message.chat.id, actor_id, fixed_project_id)
        elif command == "/bots":
            selected = await get_selected_project(
                factory, actor_id=actor_id,
                bot_scope_id=bot.id if fixed_project_id else 0,
                fixed_project_id=fixed_project_id,
            )
            if selected is None:
                await show_home(bot, message.chat.id, actor_id)
            elif fixed_project_id is None:
                await show_bots(bot, message.chat.id, actor_id, selected)
            else:
                await show_dashboard(bot, message.chat.id, actor_id, selected)
        else:
            await show_home(bot, message.chat.id, actor_id)

    @router.message(Command("cancel"), OwnerHomeFilter(factory, fixed_project_id))
    @router.message(
        F.text.in_({"✖️ Отмена", "✖️ Cancel"}),
        OwnerHomeFilter(factory, fixed_project_id),
    )
    async def cancel_setup_message(message: Message, bot: Bot) -> None:
        if not _private(message):
            return
        actor_id = await actor(message.from_user)
        await cancel_owner_flow(
            factory, actor_id=actor_id,
            bot_scope_id=bot.id if fixed_project_id else 0,
        )
        await bot.send_message(
            message.chat.id,
            tr("✖️ Настройка отменена.", "✖️ Setup canceled."),
            reply_markup=ReplyKeyboardRemove(),
        )
        await show_home(bot, message.chat.id, actor_id)

    @router.message(Command("back"), OwnerHomeFilter(factory, fixed_project_id))
    async def back_setup_message(message: Message, bot: Bot) -> None:
        """Return to the parent section, clearing only this unfinished wizard."""
        if not _private(message):
            return
        actor_id = await actor(message.from_user)
        prior = await back_owner_flow(
            factory, actor_id=actor_id,
            bot_scope_id=bot.id if fixed_project_id else 0,
        )
        if (
            prior is None or prior.project_id is None
            or (fixed_project_id is not None and prior.project_id != fixed_project_id)
        ):
            # Also clear a stale chat-picker keyboard after the 15-minute TTL.
            # The expired flow no longer has a reliable step to inspect.
            await bot.send_message(
                message.chat.id,
                tr("↩️ Возврат в главное меню.", "↩️ Back to the main menu."),
                reply_markup=ReplyKeyboardRemove(),
            )
            await show_home(bot, message.chat.id, actor_id)
            return
        project_id = prior.project_id
        if prior.step == "chat_picker":
            await bot.send_message(
                message.chat.id,
                tr("Выбор чата закрыт.", "Chat selection closed."),
                reply_markup=ReplyKeyboardRemove(),
            )
        if prior.step in ("plan_name", "plan_duration", "plan_resource"):
            await show_plans(bot, message.chat.id, actor_id, project_id)
        elif prior.step in ("price_amount", "plan_rename"):
            try:
                plan_id = UUID(hex=prior.payload["plan_id"])
            except (KeyError, TypeError, ValueError, AttributeError):
                await show_plans(bot, message.chat.id, actor_id, project_id)
            else:
                await show_plan_card(bot, message.chat.id, actor_id, project_id, plan_id)
        elif prior.step in ("method_rename", "method_edit"):
            await show_method_card(
                bot, message.chat.id, actor_id, project_id,
                UUID(hex=prior.payload["method_id"]),
            )
        elif prior.step in ("method_name", "method_instruction"):
            await show_methods(bot, message.chat.id, actor_id, project_id)
        elif prior.step in ("chat_picker", "resource_reference"):
            await show_resources(bot, message.chat.id, actor_id, project_id)
        elif prior.step in ("bot_token", "connection_rename") and fixed_project_id is None:
            await show_bots(bot, message.chat.id, actor_id, project_id)
        else:
            await show_dashboard(bot, message.chat.id, actor_id, project_id)

    @router.callback_query(F.data.startswith("own:"))
    async def owner_callback(query: CallbackQuery, bot: Bot) -> None:
        if query.message is None or query.message.chat.type != "private":
            await query.answer(tr("Откройте личный чат с ViMi Pay",
                              "Open your private chat with ViMi Pay"), show_alert=True)
            return
        user_id = query.from_user.id
        actor_id = await actor(query.from_user)
        action = (query.data or "")[4:]
        if fixed_project_id is not None and not await can_manage_project(
            factory, actor_id=actor_id, project_id=fixed_project_id,
        ):
            await query.answer(tr("Нет доступа к управлению ботом",
                              "You cannot manage this bot"), show_alert=True)
            return
        if fixed_project_id is None:
            is_registry_action = action in {
                "new", "projects", "archives", "dashboard", "bots", "connect",
                "help", "oblist", "cancel",
            } or action.startswith((
                "select:", "drop:", "dropok:", "ob:", "obok:", "obpage:",
                "projects:", "archives:", "archview:", "restore:",
                "connrename:", "connremove:", "connremoveok:",
                "connarchive:", "connarchiveok:",
            ))
            if not is_registry_action:
                await query.answer(
                    tr("Настройте продажи в своём подключённом боте",
                       "Set up sales inside your connected bot"),
                    show_alert=True,
                )
                return
        else:
            if action in {
                "new", "projects", "archives", "bots", "connect", "oblist",
            } or action.startswith((
                "select:", "drop:", "dropok:", "ob:", "obok:", "obpage:",
                "projects:", "archives:", "archview:", "restore:",
                "connrename:", "connremove:", "connremoveok:",
                "connarchive:", "connarchiveok:",
            )):

                await query.answer(
                    tr("Управляйте подключением через @vimipay_bot",
                       "Manage bot connections in @vimipay_bot"),
                    show_alert=True,
                )
                return
        try:
            if action == "new":
                if get_settings().bot_token_encryption_key is None:
                    raise ValueError(tr("Подключение ботов пока недоступно",
                                        "Bot connections are unavailable"))
                await prompt(
                    bot, user_id, actor_id, step="new_bot_token",
                    text=(
                        tr(
                            "✦ <b>Подключение бота подписок</b>\n\n"
                            "1. Создайте бота в @BotFather или получите новый "
                            "токен существующего.\n"
                            "2. Ответьте на это сообщение токеном.\n"
                            "3. Мы сразу удалим сообщение и проверим подключение.\n\n"
                            "После проверки откройте своего бота и "
                            "настройте каналы, оплату и тарифы.",
                            "✦ <b>Connect a subscription bot</b>\n\n"
                            "1. Create a bot in @BotFather or request a new token "
                            "for an existing bot.\n"
                            "2. Reply to this message with its token.\n"
                            "3. We delete the token message immediately and "
                            "verify the connection.\n\n"
                            "Then open your bot to set up channels, payments and plans.",
                        )
                    ),
                )
                await query.answer()
                return
            if action == "projects" or action.startswith("projects:"):
                page = 0
                if ":" in action:
                    value = action.split(":", 1)[1]
                    if not value.isascii() or not value.isdecimal() or len(value) > 5:
                        raise ValueError("Недопустимая страница")
                    page = int(value)
                await show_connections(bot, user_id, actor_id, page=page)
                await query.answer()
                return
            if action == "archives" or action.startswith("archives:"):
                page = 0
                if ":" in action:
                    value = action.split(":", 1)[1]
                    if not value.isascii() or not value.isdecimal() or len(value) > 5:
                        raise ValueError("Недопустимая страница")
                    page = int(value)
                await show_connections(
                    bot, user_id, actor_id, page=page, archived=True,
                )
                await query.answer()
                return
            if action.startswith("archview:"):
                await show_archived_connection(
                    bot, user_id, actor_id,
                    UUID(hex=action.split(":", 1)[1]),
                )
                await query.answer()
                return
            if action.startswith("restore:"):
                project_ref = UUID(hex=action.split(":", 1)[1])
                await restore_owner_connection(
                    factory, actor_id=actor_id, project_id=project_ref,
                )
                await bot.send_message(
                    user_id,
                    tr("✅ Подключение восстановлено.", "✅ Connection restored."),
                )
                await show_connections(bot, user_id, actor_id)
                await query.answer()
                return
            if action.startswith("select:"):
                selected = UUID(hex=action.split(":", 1)[1])
                await choose_project(
                    factory, actor_id=actor_id, project_id=selected,
                )
                await show_bots(bot, user_id, actor_id, selected)
                await query.answer()
                return
            if action == "cancel":
                await cancel_owner_flow(
                    factory, actor_id=actor_id,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                await bot.send_message(
                    user_id,
                    tr("✖️ Настройка отменена.", "✖️ Setup canceled."),
                    reply_markup=ReplyKeyboardRemove(),
                )
                await show_home(bot, user_id, actor_id)
                await query.answer()
                return
            project_id = await get_selected_project(
                factory, actor_id=actor_id,
                bot_scope_id=bot.id if fixed_project_id else 0,
                fixed_project_id=fixed_project_id,
            )
            if project_id is None:
                raise ValueError(tr("Сначала создайте или выберите проект",
                                    "Create or select a connection first"))
            if action == "dashboard":
                await show_dashboard(bot, user_id, actor_id, project_id)
            elif action.startswith("connrename:"):
                target_id = UUID(hex=action.split(":", 1)[1])
                if target_id != project_id:
                    raise ValueError("Меню другого подключения. Откройте список заново.")
                await get_connection_disposition(
                    factory, actor_id=actor_id, project_id=target_id,
                )
                await prompt(
                    bot, user_id, actor_id, step="connection_rename",
                    text=tr(
                        "✏️ <b>Новое название подключения</b>\n\n"
                        "Это название видно только в ViMi Pay. "
                        "Имя Telegram-бота в BotFather не изменится.\n"
                        "Введите название от 2 до 120 символов.",
                        "✏️ <b>Rename connection</b>\n\n"
                        "This is an internal ViMi Pay label. "
                        "It does not rename your Telegram bot.\n"
                        "Enter 2–120 characters.",
                    ),
                )
            elif action.startswith(("connremove:", "connremoveok:",
                                    "connarchive:", "connarchiveok:")):
                verb, token = action.split(":", 1)
                target_id = UUID(hex=token)
                if target_id != project_id:
                    raise ValueError("Меню другого подключения. Откройте список заново.")
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=target_id,
                )
                if not data.can_delete_bot:
                    raise PermissionError("Действие доступно только владельцу подключения")
                if verb == "connremove":
                    disposition = await get_connection_disposition(
                        factory, actor_id=actor_id, project_id=target_id,
                    )
                    if disposition.has_bot:
                        raise ValueError(
                            "Сначала отключите бота через управление подключением."
                        )
                    if disposition.has_history:
                        raise ValueError(
                            "История платежей и доступов не подлежит удалению. "
                            "Для этого проекта используйте архив."
                        )
                    await bot.send_message(
                        user_id,
                        tr("🗑 <b>Удалить подключение «", "🗑 <b>Delete connection “")
                        + escape(data.project_name)
                        + tr("»?</b>\n\n", "”?</b>\n\n")
                        + tr(
                            "Это действие необратимо. Будут удалены только "
                            "настройки НЕПОДКЛЮЧЁННОГО проекта (тарифы, "
                            "способы оплаты, каналы). Данные других ботов "
                            "и профиль сети сохранятся.\n\n"
                            "Проекты с платежами, подписчиками или историей "
                            "доступа удалить нельзя.",
                            "This cannot be undone. Configuration of this "
                            "DISCONNECTED project (plans, methods, channels) "
                            "will be deleted. Other bots and your network "
                            "profile remain untouched.\n\n"
                            "Projects with payment or access history cannot "
                            "be permanently deleted.",
                        ),
                        parse_mode="HTML",
                        reply_markup=_markup([
                            [_button(
                                tr("🗑 Да, удалить подключение",
                                   "🗑 Confirm deletion"),
                                "connremoveok:" + target_id.hex, "danger",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"), "bots")],
                        ]),
                    )
                elif verb == "connremoveok":
                    await remove_unused_connection(
                        factory, actor_id=actor_id, project_id=target_id,
                    )
                    await bot.send_message(
                        user_id, tr("✅ Подключение удалено.",
                                    "✅ Connection deleted."),
                    )
                    await show_connections(bot, user_id, actor_id)
                elif verb == "connarchive":
                    await bot.send_message(
                        user_id,
                        tr("📦 <b>Архивировать «", "📦 <b>Archive “")
                        + escape(data.project_name)
                        + tr("»?</b>\n\n", "”?</b>\n\n")
                        + tr(
                            "Подключение исчезнет из активного списка. "
                            "Тарифы и финансовая история сохранятся; "
                            "вернуть подключение можно из архива. "
                            "Действующий Telegram-бот должен быть отключён.",
                            "It will be hidden from the active list. "
                            "Plans and financial history are kept. "
                            "You can restore it from the archive. "
                            "The Telegram bot must be disconnected first.",
                        ),
                        parse_mode="HTML",
                        reply_markup=_markup([
                            [_button(
                                tr("📦 Да, архивировать", "📦 Confirm archive"),
                                "connarchiveok:" + target_id.hex, "danger",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"), "bots")],
                        ]),
                    )
                else:
                    await archive_owner_connection(
                        factory, actor_id=actor_id, project_id=target_id,
                    )
                    await bot.send_message(
                        user_id, tr("✅ Подключение перемещено в архив.",
                                    "✅ Connection moved to archive."),
                    )
                    await show_connections(bot, user_id, actor_id)
            elif action == "network":
                await show_network_profile(bot, user_id, actor_id, project_id)
            elif action == "networkask":
                await bot.send_message(
                    user_id,
                    tr(
                        "📁 <b>Сохранить настройки для новых ботов?</b>\n\n"
                        "Копируются тарифы, сроки, цены, способы оплаты и валюты. "
                        "Тарифы будут черновиками, оплата выключена.\n\n"
                        "Выберите, копировать ли также банковские реквизиты. "
                        "Автоприменение к новым ботам включится сразу.",
                        "📁 <b>Save settings for future bots?</b>\n\n"
                        "Plans, durations, prices, methods and currencies are copied. "
                        "Plans remain drafts; payments are disabled.\n\n"
                        "Choose whether to include bank details. "
                        "Auto-apply to future bots will be enabled.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("✅ С реквизитами", "✅ Include bank details"),
                            "networksave:yes", "success",
                        )],
                        [_button(
                            tr("✅ Без реквизитов", "✅ Without bank details"),
                            "networksave:no",
                        )],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"), "network")],
                    ]),
                )
            elif action.startswith("networksave:"):
                choice = action.split(":", 1)[1]
                if choice not in ("yes", "no"):
                    raise ValueError("Некорректный выбор профиля")
                saved = await save_owner_network_profile(
                    factory, actor_id=actor_id, source_project_id=project_id,
                    copy_instructions=(choice == "yes"),
                )
                await bot.send_message(
                    user_id,
                    tr("✅ Профиль сохранён: ", "✅ Profile saved: ")
                    + str(saved.plan_count)
                    + tr(" тарифов и ", " plans and ")
                    + str(saved.method_count)
                    + tr(" способов оплаты.", " payment methods."),
                )
                await show_network_profile(bot, user_id, actor_id, project_id)
            elif action in ("networkoff", "networkon"):
                await set_network_auto_apply(
                    factory, actor_id=actor_id, enabled=action == "networkon",
                )
                await show_network_profile(bot, user_id, actor_id, project_id)
            elif action == "reviews":
                await show_reviews(bot, user_id, actor_id, project_id)
            elif action == "preview":
                await show_guest_preview(bot, user_id, actor_id, project_id)
            elif action.startswith("pvcat:"):
                _, currency, page_token = action.split(":", 2)
                if not currency.isascii() or not currency.isalnum() or (
                    not page_token.isascii() or not page_token.isdecimal()
                    or int(page_token) > 10000
                ):
                    raise ValueError("Недопустимая страница")
                await show_guest_preview(
                    bot, user_id, actor_id, project_id,
                    currency=currency, page=int(page_token),
                    message_id=query.message.message_id,
                )
            elif action.startswith("pvoffer:"):
                _, price_token, page_token = action.split(":", 2)
                if not page_token.isascii() or not page_token.isdecimal() or (
                    int(page_token) > 10000
                ):
                    raise ValueError("Недопустимая страница")
                await show_guest_preview(
                    bot, user_id, actor_id, project_id,
                    price_id=decode_uuid(price_token), page=int(page_token),
                    message_id=query.message.message_id,
                )
            elif action.startswith("pvmethod:"):
                _, price_token, method_token, page_token = action.split(":", 3)
                if not page_token.isascii() or not page_token.isdecimal() or (
                    int(page_token) > 10000
                ):
                    raise ValueError("Недопустимая страница")
                await show_guest_preview(
                    bot, user_id, actor_id, project_id,
                    price_id=decode_uuid(price_token),
                    method_id=decode_uuid(method_token), page=int(page_token),
                    message_id=query.message.message_id,
                )
            elif action.startswith("pvi:"):
                _, price_token, method_token, page_token, part_token = (
                    action.split(":", 4)
                )
                if not all(
                    part.isascii() and part.isdecimal()
                    for part in (page_token, part_token)
                ) or int(page_token) > 10000 or int(part_token) > 1000:
                    raise ValueError("Недопустимая страница")
                await show_guest_preview(
                    bot, user_id, actor_id, project_id,
                    price_id=decode_uuid(price_token),
                    method_id=decode_uuid(method_token), page=int(page_token),
                    instruction_page=int(part_token),
                    message_id=query.message.message_id,
                )
            elif action.startswith("drop:"):
                _, project_ref, instance_ref = action.split(":", 2)
                if decode_uuid(project_ref) != project_id:
                    raise ValueError(tr("Меню другого проекта. Откройте «Мои боты» снова.",
                                        "Another project menu. Reopen My bots."))
                instance_id = decode_uuid(instance_ref)
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id
                )
                name = next((
                    username for bot_id, username, status in data.bot_instances
                    if bot_id == instance_id and status != "deleting"
                ), None)
                if name is None or not data.can_delete_bot:
                    raise PermissionError(tr("Этот бот не доступен для удаления",
                                            "This bot cannot be removed."))
                await bot.send_message(
                    user_id,
                    tr("🗑 <b>Удалить @", "🗑 <b>Remove @") + escape(name)
                    + "?</b>\n\n"
                    + tr(
                        "ViMi Pay отключит обработку покупателей, затем удалит "
                        "подключение, сохранённый токен и состояние запуска.\n\n"
                        "Настройки проекта и история платежей останутся: они "
                        "не принадлежат токену бота. Повторно подключить этот "
                        "Telegram-бот можно после завершения удаления.\n\n"
                        "При незавершённых оплатах или действующих подписках "
                        "удаление не начнётся, чтобы не оставить людей без "
                        "управления доступом.",
                        "ViMi Pay will stop serving buyers, then remove this "
                        "connection, saved token and runtime state.\n\n"
                        "Project settings and payment history are preserved. "
                        "You can reconnect this Telegram bot after removal finishes.\n\n"
                        "Removal will be blocked while payments or paid subscriptions "
                        "remain unsettled, so buyers do not lose access management.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            "🗑 Да, удалить бота",
                            "dropok:" + project_ref + ":" + instance_ref,
                            "danger",
                        )],
                        [_button("🧾 Управлять обязательствами", "oblist")],
                        [_button("Отмена", "bots")],
                    ]),
                )
            elif action.startswith("dropok:"):
                _, project_ref, instance_ref = action.split(":", 2)
                if decode_uuid(project_ref) != project_id:
                    raise ValueError(tr("Проект изменился. Откройте «Мои боты» снова.",
                                        "Project changed. Reopen My bots."))
                try:
                    requested = await request_bot_retirement(
                        factory, actor_id=actor_id,
                        project_id=project_id,
                        bot_instance_id=decode_uuid(instance_ref),
                    )
                except ValueError as exc:
                    # Include the entire safety explanation; Telegram alert
                    # callbacks are limited in length and would truncate it.
                    await bot.send_message(user_id, tr(
                        "⚠️ " + str(exc),
                        "⚠️ Bot removal is blocked by outstanding obligations. "
                        "Review and resolve each obligation below.",
                    ))
                    await show_obligations(bot, user_id, actor_id, project_id)
                    await query.answer(tr("Удаление пока недоступно",
                                          "Bot removal is blocked"), show_alert=True)
                    return
                await bot.send_message(
                    user_id,
                    (
                        tr(
                            "✅ Удаление запрошено. Бот прекратит обработку покупателей, "
                            "а после безопасной остановки ViMi Pay удалит его "
                            "данные подключения.\n\n"
                            "Нажмите «🔄 Обновить статус» в разделе «Мои боты». "
                            "Когда бот исчезнет из списка, его можно подключить "
                            "заново или создать новый проект с нуля.",
                            "✅ Bot removal requested. Buyer processing will stop, "
                            "then ViMi Pay will safely remove the connection.\n\n"
                            "Use Refresh status in My bots. Once removed, "
                            "you can reconnect it or start a new project.",
                        ) if requested else tr(
                            "Этот бот уже не подключён. Обновите список.",
                            "This bot is already disconnected. Refresh the list.",
                        )
                    ),
                )
                await show_bots(bot, user_id, actor_id, project_id)
            elif action == "oblist":
                await show_obligations(bot, user_id, actor_id, project_id)
            elif action.startswith("obpage:"):
                _, project_ref, page_number = action.split(":", 2)
                if decode_uuid(project_ref) != project_id:
                    raise ValueError(tr("Открыто меню другого проекта",
                                        "Another project menu is open."))
                await show_obligations(
                    bot, user_id, actor_id, project_id, page=int(page_number),
                )
            elif action.startswith(("ob:", "obok:")):
                parts = action.split(":")
                if len(parts) not in (4, 5) or parts[1] not in (
                    "pay", "sub", "grant", "sync"
                ):
                    raise ValueError(tr("Неизвестное действие", "Unknown action"))
                verb, kind, project_ref, object_ref = parts[:4]
                page = int(parts[4]) if len(parts) == 5 else 0
                if decode_uuid(project_ref) != project_id:
                    raise ValueError(tr("Открыто меню другого проекта. Обновите обязательства.",
                                        "Another project menu. Refresh obligations."))
                object_id = decode_uuid(object_ref)
                snapshot = await list_owner_obligations(
                    factory, actor_id=actor_id, project_id=project_id,
                    page=page,
                )
                active_ids = {
                    "pay": {item.id for item in snapshot.payments},
                    "sub": {item.id for item in snapshot.subscriptions},
                    "grant": {item.id for item in snapshot.grants if item.source != "payment"},
                    "sync": {item.id for item in snapshot.access},
                }
                if verb == "ob":
                    if object_id not in active_ids[kind]:
                        raise ValueError(tr("Действие уже неактуально. Обновите обязательства.",
                                        "Action is stale. Refresh obligations."))
                    warnings = {
                        "pay": tr(
                            "Проверьте поступление денег в банке. "
                            "Отклонение или отмена заявки не возвращает деньги.",
                            "Verify the bank transfer. Rejecting or closing "
                            "a payment request does not refund money.",
                        ),
                        "sub": tr(
                            "Вы досрочно закрываете оплаченный срок! "
                            "Возврат денег ViMi Pay не выполняет. "
                            "Урегулируйте вопрос с покупателем самостоятельно.",
                            "You are ending a paid term early. ViMi Pay "
                            "does not issue refunds. Resolve this with the buyer.",
                        ),
                        "grant": tr(
                            "Это отдельное право доступа. "
                            "Проверьте, не является ли оно служебным или бессрочным.",
                            "This is a separate access grant. Check whether it "
                            "is permanent or used for account management.",
                        ),
                        "sync": tr(
                            "ViMi Pay перепроверит права и отправит действие "
                            "в Telegram. Доступ не будет удалён при других действующих правах.",
                            "ViMi Pay will recalculate access and send the action "
                            "to Telegram. Other valid grants still preserve access.",
                        ),
                    }
                    await bot.send_message(
                        user_id, tr("⚠️ <b>Подтвердите действие</b>\n\n",
                                   "⚠️ <b>Confirm this action</b>\n\n")
                        + escape(warnings[kind]),
                        parse_mode="HTML", reply_markup=_markup([
                            [_button(
                                "✅ Подтверждаю действие",
                                "obok:" + kind + ":" + project_ref + ":" + object_ref
                                + (":" + str(page) if page else ""),
                                "danger" if kind != "sync" else "success",
                            )],
                            [_button("Отмена", "oblist")],
                        ]),
                    )
                else:
                    if object_id not in active_ids[kind]:
                        raise ValueError(tr("Действие уже выполнено или устарело",
                                        "Action was completed or has expired."))
                    if kind == "pay":
                        change = await close_owner_payment(
                            factory, actor_id=actor_id, project_id=project_id,
                            request_id=object_id,
                        )
                    elif kind == "sub":
                        change = await end_owner_subscription(
                            factory, actor_id=actor_id, project_id=project_id,
                            subscription_id=object_id,
                        )
                    elif kind == "grant":
                        change = await revoke_owner_grant(
                            factory, actor_id=actor_id, project_id=project_id,
                            grant_id=object_id,
                        )
                    else:
                        message = await retry_owner_access(
                            factory, actor_id=actor_id, project_id=project_id,
                            state_id=object_id,
                        )
                        await bot.send_message(user_id, "🔄 " + obligation_message(message))
                        change = None
                    if change is not None:
                        await bot.send_message(
                            user_id, "✅ " + (
                                tr("Состояние сохранено", "Change saved")
                                if change.changed else obligation_message(change.message)
                            ),
                        )
                        await notify_buyer(bot, user_id, actor_id, project_id, change)
                    await show_obligations(bot, user_id, actor_id, project_id)
            elif action == "bots":
                await show_bots(bot, user_id, actor_id, project_id)
            elif action == "connect":
                if get_settings().bot_token_encryption_key is None:
                    raise ValueError(tr("Подключение ботов временно недоступно",
                                        "Bot connections are temporarily unavailable"))
                await prompt(
                    bot, user_id, actor_id, step="bot_token",
                    text=(
                        tr(
                            "🤖 <b>Подключение вашего бота</b>\n\n"
                            "1. Создайте бота в @BotFather.\n"
                            "2. Ответьте на это сообщение токеном.\n"
                            "3. ViMi Pay сразу удалит ваше сообщение и проверит токен.\n\n"
                            "Бот пока не начнёт принимать покупателей.",
                            "🤖 <b>Connect your bot</b>\n\n"
                            "1. Create a bot in @BotFather.\n"
                            "2. Reply to this message with its token.\n"
                            "3. ViMi Pay deletes your message immediately and verifies "
                            "the token.\n\n"
                            "Buyers cannot purchase until setup is finished.",
                        )
                    ),
                )
            elif action == "resources":
                await show_resources(bot, user_id, actor_id, project_id)
            elif action.startswith("pick:"):
                kind = action.split(":", 1)[1]
                if kind not in ("channel", "group"):
                    raise ValueError(tr("Неизвестный тип чата", "Unsupported chat type"))
                request_id = randbelow(2**31 - 2) + 1
                await set_flow(
                    factory, actor_id=actor_id,
                    step="chat_picker", payload={"request_id": request_id},
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                await bot.send_message(
                    user_id,
                    tr(
                        "📢 Выберите ваш канал или группу с помощью кнопки ниже.\n"
                        "Убедитесь, что ваш подключённый бот уже добавлен "
                        "администратором с правами приглашения и блокировки.",
                        "📢 Choose a channel or group using the button below.\n"
                        "Your connected bot must already be an administrator "
                        "with permission to invite and ban members.",
                    ),
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[
                            [KeyboardButton(
                                text=tr("📢 Выбрать чат", "📢 Choose chat"),
                                request_chat=KeyboardButtonRequestChat(
                                    request_id=request_id,
                                    chat_is_channel=(kind == "channel"),
                                    request_title=True,
                                ),
                            )],
                            [KeyboardButton(text=tr("✖️ Отмена", "✖️ Cancel"))],
                        ],
                        resize_keyboard=True,
                        one_time_keyboard=True,
                    ),
                )
            elif action == "resource:manual":
                await prompt(
                    bot, user_id, actor_id, step="resource_reference",
                    text=tr(
                        "📢 Отправьте ID канала (например, -100…) или @username.\n"
                        "Ваш собственный бот уже должен быть администратором.",
                        "📢 Send the channel ID (for example, -100…) or @username.\n"
                        "Your bot must already be a channel administrator.",
                    ),
                )
            elif action == "plans":
                await show_plans(bot, user_id, actor_id, project_id)
            elif action == "newplan":
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id
                )
                if not any(status == "active" for _, _, status in data.resources):
                    raise ValueError(tr("Сначала подключите действующий канал или группу",
                                        "Connect an active channel or group first"))
                await prompt(
                    bot, user_id, actor_id, step="plan_name",
                    text=tr("📦 Как назвать новый тариф?\nНапример: Premium",
                             "📦 What is the new plan's name?\nFor example: Premium"),
                )
            elif action.startswith("flowback:"):
                # A stale keyboard must never reset a newer wizard.
                _, target, generation = action.split(":", 2)
                if target not in ("plans", "duration"):
                    raise ValueError(tr("Шаг настройки устарел",
                                        "This setup step has expired"))
                flow = await consume_button_flow(
                    factory, actor_id=actor_id,
                    expected_step=(
                        "plan_duration" if target == "plans" else "plan_resource"
                    ),
                    generation=generation,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                    discard=(target == "plans"),
                )
                if flow.project_id != project_id:
                    raise ValueError(tr(
                        "Проект изменился. Повторите действие.",
                        "Project changed. Please retry.",
                    ))
                if target == "plans":
                    # The button step and generation are already invalidated
                    # atomically by consume_button_flow(discard=True).
                    await show_plans(bot, user_id, actor_id, project_id)
                else:
                    fresh = await advance_flow(
                        factory, actor_id=actor_id, source=flow,
                        step="plan_duration",
                        payload={"name": flow.payload["name"]},
                        bot_scope_id=bot.id if fixed_project_id else 0,
                    )
                    await show_duration_picker(bot, user_id, fresh)
            elif action.startswith("duration:"):
                _, key, generation = action.split(":", 2)
                if key not in DURATIONS:
                    raise ValueError(tr("Неподдерживаемый срок", "Unsupported duration"))
                flow = await consume_button_flow(
                    factory, actor_id=actor_id,
                    expected_step="plan_duration", generation=generation,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                if flow.project_id != project_id:
                    raise ValueError(tr("Проект изменился. Повторите действие.",
                                            "Project changed. Please retry."))
                value, unit, _ = DURATIONS[key]
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id
                )
                resources = [
                    rid for rid, _, status in data.resources if status == "active"
                ]
                if not resources:
                    raise ValueError(tr("Сначала подключите канал", "Connect a channel first"))
                if len(resources) == 1:
                    plan_id = await create_owner_plan(
                        factory, actor_id=actor_id, project_id=project_id,
                        name=flow.payload["name"], duration_value=value,
                        duration_unit=unit, resource_id=resources[0],
                    )
                    await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
                else:
                    generation = await advance_flow(
                        factory, actor_id=actor_id, source=flow,
                        step="plan_resource",
                        payload={"name": flow.payload["name"], "value": value, "unit": unit},
                        bot_scope_id=bot.id if fixed_project_id else 0,
                    )
                    rows = [
                        [_button(name[:45], "use:" + rid.hex + ":" + generation)]
                        for rid, name, status in data.resources if status == "active"
                    ]
                    await bot.send_message(
                        user_id, tr("📢 Какой канал откроет этот тариф?",
                                     "📢 Which channel does this plan unlock?"),
                        reply_markup=_markup(
                            rows + [
                                [_button(
                                    tr("⬅️ Выбор срока", "⬅️ Choose duration"),
                                    "flowback:duration:" + generation,
                                )],
                                [_button("✖️ Отмена", "cancel")],
                            ],
                        ),
                    )
            elif action.startswith("use:"):
                _, resource_hex, generation = action.split(":", 2)
                flow = await consume_button_flow(
                    factory, actor_id=actor_id,
                    expected_step="plan_resource", generation=generation,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                if flow.project_id != project_id:
                    raise ValueError(tr("Проект изменился. Повторите действие.",
                                            "Project changed. Please retry."))
                plan_id = await create_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    name=flow.payload["name"], duration_value=flow.payload["value"],
                    duration_unit=flow.payload["unit"],
                    resource_id=UUID(hex=resource_hex),
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("plan:"):
                await show_plan_card(
                    bot, user_id, actor_id, project_id,
                    UUID(hex=action.split(":", 1)[1]),
                )
            elif action.startswith("plrename:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                data = await get_owner_plan_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                )
                warning = tr(
                    "\n⚠️ Новое название также будет видно в истории покупок.",
                    "\n⚠️ The new name will also appear in purchase history.",
                ) if data.has_history else ""
                await prompt(
                    bot, user_id, actor_id, step="plan_rename",
                    payload={"plan_id": plan_id.hex},
                    text=tr(
                        "✏️ <b>Переименовать тариф</b>\n\n"
                        "Введите новое название (2–120 символов).",
                        "✏️ <b>Rename plan</b>\n\n"
                        "Enter the new name (2–120 characters).",
                    ) + warning,
                )
            elif action.startswith("plduration:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                await show_plan_duration_editor(
                    bot, user_id, actor_id, project_id, plan_id,
                )
            elif action.startswith("plsetperiod:"):
                _, token, key = action.split(":", 2)
                if key not in DURATIONS:
                    raise ValueError(tr("Недопустимый срок", "Invalid duration"))
                plan_id = decode_uuid(token)
                value, unit, _ = DURATIONS[key]
                await edit_owner_draft_duration(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, duration_value=value, duration_unit=unit,
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("plresource:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                await show_plan_resource_editor(
                    bot, user_id, actor_id, project_id, plan_id,
                )
            elif action.startswith("plsetres:"):
                _, plan_token, resource_token = action.split(":", 2)
                plan_id = decode_uuid(plan_token)
                await edit_owner_draft_resource(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, resource_id=decode_uuid(resource_token),
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("plarchive:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                info = await get_owner_plan_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                )
                if info.status != "active":
                    raise ValueError("Снять с продажи можно только активный тариф")
                await bot.send_message(
                    user_id,
                    tr(
                        "⏸️ <b>Снять тариф с продажи?</b>\n\n"
                        "Покупатели больше не смогут выбрать его для новых заявок. "
                        "Существующие подписки, уже созданные заявки и "
                        "история платежей останутся без изменений.",
                        "⏸️ <b>Stop selling this plan?</b>\n\n"
                        "Buyers will no longer be able to select it for new "
                        "requests. Existing subscriptions, payment requests "
                        "and payment history will remain unchanged.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("⏸️ Да, снять с продажи", "⏸️ Confirm stop"),
                            "plarchok:" + encode_uuid(plan_id) + ":" + info.revision,
                            "danger",
                        )],
                        [_button(
                            tr("⬅️ Оставить тариф", "⬅️ Keep plan"),
                            "plan:" + plan_id.hex,
                        )],
                    ]),
                )
            elif action.startswith("plarchok:"):
                _, token, revision = action.split(":", 2)
                plan_id = decode_uuid(token)
                await archive_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, expected_revision=revision,
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("plrestore:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                info = await get_owner_plan_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                )
                if info.status != "archived":
                    raise ValueError("Тариф изменился. Откройте карточку заново.")
                await bot.send_message(
                    user_id,
                    tr(
                        "▶️ <b>Вернуть тариф в продажу?</b>\n\n"
                        "Он снова появится в каталоге, если цены и оплата настроены.",
                        "▶️ <b>Resume selling this plan?</b>\n\n"
                        "It will reappear in the catalog if prices and payments are ready.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("✅ Возобновить продажи", "✅ Resume sales"),
                            "plrestoreok:" + encode_uuid(plan_id) + ":" + info.revision,
                            "success",
                        )],
                        [_button(
                            tr("⬅️ Оставить в архиве", "⬅️ Keep archived"),
                            "plan:" + plan_id.hex,
                        )],
                    ]),
                )
            elif action.startswith("plrestoreok:"):
                _, token, revision = action.split(":", 2)
                plan_id = decode_uuid(token)
                await publish_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, expected_status="archived",
                    expected_revision=revision,
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("pldelete:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                info = await get_owner_plan_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                )
                if info.has_history:
                    raise ValueError(
                        "Тариф имеет историю заявок или подписок. "
                        "Удаление запрещено — снимите его с продажи."
                    )
                status_code = {"draft": "d", "active": "a", "archived": "r"}[
                    info.status
                ]
                await bot.send_message(
                    user_id,
                    tr(
                        "🗑 <b>Удалить тариф навсегда?</b>\n\n",
                        "🗑 <b>Delete this plan permanently?</b>\n\n",
                    )
                    + "<b>" + escape(info.name) + "</b>\n\n"
                    + tr(
                        "Будут удалены настройки цен и привязка к каналу. "
                        "Это возможно только если по тарифу никогда не было "
                        "заявок и подписок.",
                        "Its prices and channel assignment will be removed. "
                        "Deletion is allowed only if there have never been "
                        "payment requests or subscriptions for this plan.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("🗑 Да, удалить тариф", "🗑 Yes, delete plan"),
                            "pldelok:" + encode_uuid(plan_id) + ":" + status_code
                            + ":" + info.revision,
                            "danger",
                        )],
                        [_button(
                            tr("⬅️ Отмена", "⬅️ Cancel"),
                            "plan:" + plan_id.hex,
                        )],
                    ]),
                )
            elif action.startswith("pldelok:"):
                _, token, code, revision = action.split(":", 3)
                expected = {"d": "draft", "a": "active", "r": "archived"}.get(
                    code
                )
                if expected is None:
                    raise ValueError("Тариф изменился. Откройте карточку заново.")
                await delete_unused_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=decode_uuid(token), expected_status=expected,
                    expected_revision=revision,
                )
                await bot.send_message(
                    user_id,
                    tr("✅ Тариф удалён.", "✅ Plan deleted."),
                )
                await show_plans(bot, user_id, actor_id, project_id)
            elif action.startswith("price:"):
                _, plan_hex, currency = action.split(":")
                if currency not in ALLOWED_CURRENCIES:
                    raise ValueError(tr("Неизвестная валюта", "Unsupported currency"))
                await prompt(
                    bot, user_id, actor_id, step="price_amount",
                    payload={"plan_id": plan_hex, "currency": currency},
                    text=tr(
                        f"💰 Укажите цену в {currency} (например, 499 или 9.99):",
                        f"💰 Enter the price in {currency} (e.g. 499 or 9.99):",
                    ),
                )
            elif action.startswith("publish:"):
                # Old pre-management publish buttons are intentionally stale:
                # publication requires the draft's current plan revision.
                _, token, revision = action.split(":", 2)
                plan_id = decode_uuid(token)
                try:
                    name = await publish_owner_plan(
                        factory, actor_id=actor_id, project_id=project_id,
                        plan_id=plan_id, expected_status="draft",
                        expected_revision=revision,
                    )
                except ValueError as exc:
                    if "Добавьте действующий способ оплаты" not in str(exc):
                        raise
                    await bot.send_message(
                        user_id,
                        tr(
                            "💳 <b>Остался один шаг</b>\n\n"
                            "Добавьте способ оплаты, который поддерживает "
                            "валюту тарифа. Черновик и цены сохранены.",
                            "💳 <b>One step left</b>\n\n"
                            "Add a payment method supporting this plan's "
                            "currency. Your draft and prices are saved.",
                        ),
                        parse_mode="HTML",
                        reply_markup=_markup([
                            [_button(
                                "➕ Настроить способ оплаты",
                                "newmethod", "success",
                            )],
                            [_button("⬅️ Вернуться к тарифу", "plan:" + plan_id.hex)],
                        ]),
                    )
                    await query.answer()
                    return
                await bot.send_message(
                    user_id, tr(f"✅ Тариф «{escape(name)}» опубликован.",
                                 f"✅ Plan “{escape(name)}” published."),
                    parse_mode="HTML",
                )
                await show_plans(bot, user_id, actor_id, project_id)
            elif action == "methods":
                await show_methods(bot, user_id, actor_id, project_id)
            elif action.startswith("methodpage:"):
                page = int(action.split(":", 1)[1])
                await show_methods(
                    bot, user_id, actor_id, project_id, page=page,
                )
            elif action == "newmethod":
                await currency_picker(
                    bot, user_id, actor_id, mode="method"
                )
            elif action.startswith("method:"):
                await show_method_card(
                    bot, user_id, actor_id, project_id,
                    UUID(hex=action.split(":", 1)[1]),
                )
            elif action.startswith("methodlink:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                method = await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                codes = await owner_available_currencies(
                    factory, actor_id=actor_id,
                )
                rows = [
                    [_button(
                        "➕ " + code,
                        "methodattach:" + method_id.hex + ":" + code,
                    )] for code in codes if code not in method.currencies
                ]
                rows.extend([
                    [_button(
                        tr("➖ " + code, "➖ " + code),
                        "methodremoveask:" + encode_uuid(method_id) + ":" + code,
                        "danger",
                    )] for code in method.currencies
                    if len(method.currencies) > 1
                ])
                if not rows:
                    await bot.send_message(
                        user_id, tr("Все доступные валюты уже подключены.",
                                     "All available currencies are already enabled."),
                        reply_markup=_markup([[_button(
                            tr("⬅️ К способу оплаты", "⬅️ Payment method"),
                            "method:" + method_id.hex,
                        )]]),
                    )
                else:
                    rows.append([
                        _button("⬅️ Вернуться", "method:" + method_id.hex),
                    ])
                    await bot.send_message(
                        user_id,
                        "🌐 <b>" + escape(method.name) + "</b>\n\n"
                        + tr("Выберите дополнительную валюту. "
                             "Текущие подключения сохранятся.",
                             "Select another currency. "
                             "Existing currencies will remain enabled."),
                        parse_mode="HTML", reply_markup=_markup(rows),
                    )
            elif action.startswith("methodattach:"):
                _, method_hex, currency = action.split(":", 2)
                added = await attach_owner_method_currency(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=UUID(hex=method_hex), currency_code=currency,
                )
                await bot.send_message(
                    user_id,
                    tr("✅ Валюта подключена", "✅ Currency enabled") if added
                    else tr("Эта валюта уже подключена.",
                            "This currency is already enabled."),
                )
                await show_method_card(
                    bot, user_id, actor_id, project_id, UUID(hex=method_hex),
                )
            elif action.startswith("methodrename:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                await prompt(
                    bot, user_id, actor_id, step="method_rename",
                    payload={"method_id": method_id.hex},
                    text=tr(
                        "✏️ <b>Новое название способа оплаты</b>\n"
                        "От 2 до 120 символов.",
                        "✏️ <b>Rename payment method</b>\n"
                        "Enter 2–120 characters.",
                    ),
                )
            elif action.startswith("methodedit:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                await prompt(
                    bot, user_id, actor_id, step="method_edit",
                    payload={"method_id": method_id.hex},
                    text=tr(
                        "📝 <b>Новые реквизиты и инструкция</b>\n"
                        "Допускаются {amount}, {currency}, {plan}, {period}.\n"
                        "Новые инструкции увидят покупатели при открытии оплаты.",
                        "📝 <b>New payment instructions</b>\n"
                        "Allowed: {amount}, {currency}, {plan}, {period}.\n"
                        "Buyers will see the new text when opening payment.",
                    ),
                )
            elif action.startswith("methodremoveask:"):
                _, token, code = action.split(":", 2)
                method_id = decode_uuid(token)
                method = await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                if code not in method.currencies or len(method.currencies) < 2:
                    raise ValueError(tr(
                        "Эту валюту отключить нельзя.",
                        "This currency cannot be detached.",
                    ))
                await bot.send_message(
                    user_id,
                    tr("➖ Отключить валюту ", "➖ Detach currency ") + code
                    + tr(" от способа «", " from “")
                    + escape(method.name) + tr("»?", "”?"),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("➖ Да, отключить валюту", "➖ Confirm detach"),
                            "methodremove:" + token + ":" + code + ":" + method.revision,
                            "danger",
                        )],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                 "method:" + method_id.hex)],
                    ]),
                )
            elif action.startswith("methodremove:"):
                _, token, code, revision = action.split(":", 3)
                method_id = decode_uuid(token)
                await detach_owner_method_currency(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, currency_code=code,
                    expected_revision=revision,
                )
                await bot.send_message(
                    user_id, tr("✅ Валюта отключена.", "✅ Currency detached."),
                )
                await show_method_card(bot, user_id, actor_id, project_id, method_id)
            elif action.startswith("methoddeleteask:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                method = await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                if method.has_history:
                    raise ValueError(tr(
                        "Есть история покупок. Отключите способ оплаты.",
                        "Purchase history exists. Disable the method instead.",
                    ))
                await bot.send_message(
                    user_id,
                    tr("🗑 <b>Удалить способ оплаты навсегда?</b>\n\n",
                       "🗑 <b>Permanently delete payment method?</b>\n\n")
                    + escape(method.name)
                    + tr(
                        "\n\nНастройки и валюты будут удалены. "
                        "Удаление возможно только без истории заявок и платежей.",
                        "\n\nSettings and currency links will be deleted. "
                        "Only methods with no request or payment history can be removed.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("🗑 Да, удалить", "🗑 Confirm deletion"),
                            "methoddelete:" + encode_uuid(method_id) + ":" + method.revision,
                            "danger",
                        )],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                 "method:" + method_id.hex)],
                    ]),
                )
            elif action.startswith("methoddelete:"):
                _, token, revision = action.split(":", 2)
                method_id = decode_uuid(token)
                await delete_unused_owner_method(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, expected_revision=revision,
                )
                await bot.send_message(
                    user_id, tr("✅ Способ оплаты удалён.", "✅ Payment method deleted."),
                )
                await show_methods(bot, user_id, actor_id, project_id)
            elif action.startswith("methodconfirmoff:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                method = await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                if method.status != "active":
                    raise ValueError(tr(
                        "Способ уже отключён.", "Method is already disabled.",
                    ))
                await bot.send_message(
                    user_id,
                    tr(
                        "⏸️ <b>Отключить способ оплаты?</b>\n\n"
                        "Он исчезнет из новых покупок. Уже полученные "
                        "заявки и история платежей сохранятся.",
                        "⏸️ <b>Disable this payment method?</b>\n\n"
                        "It will no longer be offered for new purchases. "
                        "Existing payment requests and history are preserved.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("⏸️ Подтвердить отключение", "⏸️ Confirm disable"),
                            "methodoff:" + encode_uuid(method_id) + ":" + method.revision,
                            "danger",
                        )],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                 "method:" + method_id.hex)],
                    ]),
                )
            elif action.startswith(("methodoff:", "methodon:")):
                is_active = action.startswith("methodon:")
                _, token, revision = action.split(":", 2)
                method_id = decode_uuid(token)
                await change_owner_method_status_guarded(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, enabled=is_active,
                    expected_revision=revision,
                )
                await show_method_card(bot, user_id, actor_id, project_id, method_id)
            elif action.startswith("methodcurrency:"):
                code = action.split(":", 1)[1]
                if code not in await owner_available_currencies(
                    factory, actor_id=actor_id
                ):
                    raise ValueError(tr("Неизвестная валюта", "Unsupported currency"))
                await prompt(
                    bot, user_id, actor_id, step="method_name",
                    payload={"currency": code},
                    text=tr(
                        f"💳 Название способа оплаты для {code}:\n"
                        "Например: СБП или Банковский перевод.",
                        f"💳 Payment method name for {code}:\n"
                        "For example: Bank transfer.",
                    ),
                )
            elif action == "timezone":
                rows = [
                    [_button(zone, "tz:" + zone + ":" + project_id.hex)]
                    for zone in (
                        "Europe/Moscow", "Europe/Amsterdam",
                        "Europe/London", "Asia/Bangkok", "Asia/Dubai"
                    )
                ]
                rows.append([_button("✍️ Указать другой", "tzcustom")])
                rows.append([_button(
                    tr("⬅️ Панель администратора", "⬅️ Admin dashboard"),
                    "dashboard",
                )])
                await bot.send_message(
                    user_id, tr("🌍 Выберите часовой пояс:", "🌍 Choose a timezone:"),
                    reply_markup=_markup(rows),
                )
            elif action == "tzcustom":
                await prompt(
                    bot, user_id, actor_id, step="timezone",
                    text=tr(
                        "🌍 Отправьте часовой пояс IANA, например Europe/Moscow.",
                        "🌍 Send an IANA timezone, for example Europe/London.",
                    ),
                )
            elif action.startswith("tz:"):
                _, value, origin_hex = action.split(":", 2)
                if UUID(hex=origin_hex) != project_id:
                    raise ValueError(tr(
                        "Это меню другого проекта. Откройте настройки заново.",
                        "This menu belongs to another project. Reopen settings.",
                    ))
                await change_project_timezone(
                    factory, project_id=project_id,
                    actor_telegram_user_id=user_id, timezone_name=value,
                )
                await show_dashboard(bot, user_id, actor_id, project_id)
            elif action == "readiness":
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                next_step = _next_setup_step(data)
                rows = []
                if next_step is not None:
                    rows.append([_button(
                        next_step[0], next_step[1], "success",
                    )])
                rows.append([_button(
                    "⬅️ Панель администратора", "dashboard",
                )])
                await bot.send_message(
                    user_id,
                    tr("📋 <b>Готовность к продажам</b>\n\n",
                       "📋 <b>Sales readiness</b>\n\n")
                    + "\n".join(_progress(data))
                    + "\n\n🤖 " + escape(owner_runtime_label(data.bot_runtime_label))
                    + "\n\n"
                    + (
                        tr("✅ Тарифы доступны для покупки.",
                           "✅ Plans are available to buyers.")
                        if data.sales_configured
                        else tr("Бот уже может принимать настройки. "
                                "Покупатели увидят тарифы после завершения шагов.",
                                "The bot is ready for configuration. "
                                "Buyers will see plans after setup is complete.")
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup(rows),
                )
            elif action == "help":
                await show_help(bot, user_id)
            else:
                raise ValueError(tr("Неизвестное действие", "Unknown action"))
        except (ValueError, PermissionError) as exc:
            detail = (
                obligation_error(str(exc))
                if action.startswith(("ob:", "obok:", "obpage:"))
                else owner_error(str(exc))
            )
            await query.answer(detail[:180], show_alert=True)
            return
        await query.answer()

    @router.message(F.chat_shared)
    async def shared_chat_handler(message: Message, bot: Bot) -> None:
        if not _private(message) or fixed_project_id is None:
            return
        actor_id = await actor(message.from_user)
        if not await can_manage_project(
            factory, actor_id=actor_id, project_id=fixed_project_id,
        ):
            return
        try:
            flow = await consume_button_flow(
                factory, actor_id=actor_id, expected_step="chat_picker",
                request_id=message.chat_shared.request_id,
                bot_scope_id=bot.id,
            )
            if flow.project_id != fixed_project_id:
                raise ValueError(tr("Настройка другого бота недоступна",
                                    "You cannot change another bot's settings"))
            chat = await verify_channel_and_add(
                factory, actor_id=actor_id,
                actor_telegram_id=message.from_user.id,
                project_id=flow.project_id,
                chat_reference=message.chat_shared.chat_id,
            )
        except (ValueError, PermissionError) as exc:
            await bot.send_message(
                message.chat.id, "⚠️ " + owner_error(str(exc)),
                reply_markup=ReplyKeyboardRemove(),
            )
            await bot.send_message(
                message.chat.id,
                tr("Вернуться к управлению каналами",
                   "Return to channel management"),
                reply_markup=_markup([[_button(
                    tr("⬅️ Каналы и группы", "⬅️ Channels and groups"),
                    "resources",
                )]]),
            )
            return
        await bot.send_message(
            message.chat.id,
            tr("✅ Канал «" + escape(chat.name) + "» подключён.",
                       "✅ Channel “" + escape(chat.name) + "” connected."),
            parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
        )
        await show_resources(bot, message.chat.id, actor_id, flow.project_id)

    async def send_connected_bot(
        bot: Bot, user_id: int, actor_id: UUID,
        project_id: UUID, username: str,
    ) -> None:
        imported = await get_project_import_status(
            factory, actor_id=actor_id, project_id=project_id,
        )
        details = (
            tr(
                "\n\n📁 Профиль сети применён: тарифы созданы как черновики, "
                "способы оплаты выключены. Привяжите канал, проверьте "
                "реквизиты и включите продажи.",
                "\n\n📁 Network profile applied: plans are drafts and "
                "payments are disabled. Connect a channel, verify bank "
                "details and enable sales.",
            ) if imported == "applied" else (
                tr(
                    "\n\n⚠️ Настройки профиля не скопированы. Бот подключён "
                    "без шаблона, настройте его вручную.",
                    "\n\n⚠️ The profile could not be imported. The bot "
                    "connected without the template; configure it manually.",
                ) if imported == "failed" else ""
            )
        )
        await bot.send_message(
            user_id,
            tr(
                f"✅ <b>@{escape(username)} подключён</b>\n\n"
                "Настройка в ViMi Pay завершена.\n"
                "Перейдите в свой бот и добавьте канал, "
                "способы оплаты и тарифы.",
                f"✅ <b>@{escape(username)} connected</b>\n\n"
                "Your bot is connected to ViMi Pay.\n"
                "Open it to configure channels, payment methods and plans.",
            ) + details,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr("🚀 Перейти к своему боту", "🚀 Open my bot"),
                    url="https://t.me/" + username,
                    style="success",
                ),
            ]]),
        )
        await show_bots(bot, user_id, actor_id, project_id)

    async def finish_bot_connection(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID, token: str,
    ) -> None:
        result = await register_customer_bot(
            factory, project_id=project_id,
            actor_telegram_user_id=user_id, token=token,
            encryption_key=get_settings().bot_token_encryption_key,
            central_bot_id=bot.id,
        )
        await set_default_project_name_from_bot(
            factory, actor_id=actor_id, project_id=project_id,
            username=result.username,
        )
        await send_connected_bot(
            bot, user_id, actor_id, project_id, result.username,
        )

    @router.message(SetupReplyFilter(factory, fixed_project_id))
    async def owner_reply_handler(message: Message, bot: Bot) -> None:
        if not _private(message) or message.reply_to_message is None:
            return
        if (
            message.reply_to_message.from_user is None
            or message.reply_to_message.from_user.id != bot.id
        ):
            return
        actor_id = await actor(message.from_user)
        text = (message.text or "").strip()
        flow = await consume_flow(
            factory, actor_id=actor_id,
            reply_message_id=message.reply_to_message.message_id,
            bot_scope_id=bot.id if fixed_project_id else 0,
        )
        if flow is None:
            return
        # Always delete BotFather credential messages before validating or storing
        # anything, even if the credential was typed incorrectly.
        unexpected_secret = bool(
            flow.step not in ("bot_token", "new_bot_token")
            and TOKEN_PATTERN.fullmatch(text)
        )
        if flow.step in ("bot_token", "new_bot_token") or unexpected_secret:
            try:
                deleted = await bot.delete_message(
                    message.chat.id, message.message_id
                )
            except AiogramError:
                deleted = False
            if not deleted:
                await bot.send_message(
                    message.chat.id,
                    tr("⚠️ Не удалось удалить токен. Смените его в BotFather.",
                       "⚠️ Could not delete your bot token. Rotate it in BotFather."),
                )
                return
            if unexpected_secret:
                await bot.send_message(
                    message.chat.id,
                    tr("⚠️ Вы прислали токен вместо запрошенных данных. "
                       "Для безопасности смените токен в BotFather и повторите шаг.",
                       "⚠️ You sent a bot token instead of the requested information. "
                       "Rotate your token in BotFather and retry."),
                )
                return
        # Old central setup prompts remain in Telegram after a deployment.
        # Do not permit completing tenant settings via stale central messages.
        if fixed_project_id is None and flow.step not in (
            "new_bot_token", "bot_token", "connection_rename",
        ):
            await bot.send_message(
                message.chat.id,
                tr("Настройка перенесена в вашего бота. "
                   "Откройте его через «Мои боты».",
                   "Setup has moved to your connected bot. "
                   "Open it from My bots."),
            )
            return
        if fixed_project_id is not None and flow.step in (
            "new_bot_token", "bot_token", "project_name", "connection_rename",
        ):
            await bot.send_message(
                message.chat.id,
                tr("Подключение токена доступно только в @vimipay_bot.",
                   "Bot tokens can only be connected through @vimipay_bot."),
            )
            return
        try:
            if flow.step == "new_bot_token":
                if not TOKEN_PATTERN.fullmatch(text):
                    raise ValueError(
                        tr("Неверный формат токена. Получите токен в @BotFather.",
                           "Invalid token format. Get a token from @BotFather.")
                    )
                project_id, registration = await register_new_owner_bot(
                    factory, actor_id=actor_id,
                    actor_telegram_user_id=message.from_user.id,
                    token=text,
                    encryption_key=get_settings().bot_token_encryption_key,
                    central_bot_id=bot.id,
                )
                await send_connected_bot(
                    bot, message.chat.id, actor_id,
                    project_id, registration.username,
                )
                return
            if flow.step == "project_name":
                await create_owner_project(factory, actor_id=actor_id, name=text)
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Проект создан. Часовой пояс — Москва (можно изменить).",
                       "✅ Project created. Default timezone: Moscow (editable)."),
                )
                await show_home(bot, message.chat.id, actor_id)
                return
            project_id = flow.project_id
            if project_id is None or (
                fixed_project_id is not None and project_id != fixed_project_id
            ):
                raise ValueError(tr("Настройка другого бота недоступна",
                                    "You cannot change another bot's settings"))
            if flow.step == "connection_rename":
                if fixed_project_id is not None:
                    raise PermissionError("Переименование доступно в центральном боте")
                current = await get_selected_project(factory, actor_id=actor_id)
                if current != project_id:
                    raise ValueError("Подключение изменилось. Откройте список заново.")
                renamed = await rename_owner_connection(
                    factory, actor_id=actor_id, project_id=project_id,
                    name=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Подключение переименовано: ",
                       "✅ Connection renamed: ") + escape(renamed),
                    parse_mode="HTML",
                )
                await show_bots(bot, message.chat.id, actor_id, project_id)
            elif flow.step == "bot_token":
                if not TOKEN_PATTERN.fullmatch(text):
                    raise ValueError(tr("Неверный формат токена. Получите его в BotFather.",
                                        "Invalid token format. Get it from BotFather."))
                await finish_bot_connection(
                    bot, message.chat.id, actor_id, project_id, text,
                )
            elif flow.step == "resource_reference":
                reference: str | int = int(text) if text.lstrip("-").isdigit() else text
                resource = await verify_channel_and_add(
                    factory, actor_id=actor_id,
                    actor_telegram_id=message.from_user.id,
                    project_id=project_id, chat_reference=reference,
                )
                await bot.send_message(
                    message.chat.id,
                    tr(f"✅ Канал «{escape(resource.name)}» подключён.",
                       f"✅ Channel “{escape(resource.name)}” connected."),
                    parse_mode="HTML",
                )
                await show_resources(bot, message.chat.id, actor_id, project_id)
            elif flow.step == "plan_name":
                if not 2 <= len(text) <= 120:
                    raise ValueError(tr("Название тарифа: от 2 до 120 символов",
                                        "Plan name must be 2–120 characters"))
                generation = await advance_flow(
                    factory, actor_id=actor_id, source=flow,
                    step="plan_duration", payload={"name": text},
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                await show_duration_picker(
                    bot, message.chat.id, generation,
                )
            elif flow.step == "plan_rename":
                plan_id = UUID(hex=flow.payload["plan_id"])
                name = await rename_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, name=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr(
                        f"✅ Тариф переименован в «{escape(name)}».",
                        f"✅ Plan renamed to “{escape(name)}”.",
                    ),
                    parse_mode="HTML",
                )
                await show_plan_card(
                    bot, message.chat.id, actor_id, project_id, plan_id,
                )
            elif flow.step == "price_amount":
                plan_id = UUID(hex=flow.payload["plan_id"])
                await save_owner_price(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                    currency_code=flow.payload["currency"], amount=text,
                )
                await bot.send_message(message.chat.id, tr("✅ Цена сохранена.", "✅ Price saved."))
                await show_plan_card(bot, message.chat.id, actor_id, project_id, plan_id)
            elif flow.step == "method_rename":
                method_id = UUID(hex=flow.payload["method_id"])
                name = await rename_owner_method(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, name=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Способ переименован.", "✅ Payment method renamed."),
                )
                await show_method_card(
                    bot, message.chat.id, actor_id, project_id, method_id,
                )
            elif flow.step == "method_edit":
                method_id = UUID(hex=flow.payload["method_id"])
                await edit_owner_method_instructions(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, instruction=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Реквизиты обновлены.", "✅ Instructions updated."),
                )
                await show_method_card(
                    bot, message.chat.id, actor_id, project_id, method_id,
                )
            elif flow.step == "method_name":
                if not 2 <= len(text) <= 120:
                    raise ValueError(tr("Название способа оплаты: от 2 до 120 символов",
                                        "Payment method name must be 2–120 characters"))
                await prompt(
                    bot, message.chat.id, actor_id,
                    step="method_instruction",
                    source_flow=flow,
                    payload={"currency": flow.payload["currency"], "name": text},
                    text=(
                        tr(
                            "💳 Отправьте инструкцию покупателю: куда и как переводить "
                            "деньги.\n\nМожно использовать {amount}, {currency}, "
                            "{plan}, {period}.\n"
                            "Например: Переведите {amount} {currency} по СБП на ...",
                            "💳 Send the instructions that buyers should follow.\n\n"
                            "You can use {amount}, {currency}, {plan}, {period}.\n"
                            "For example: Transfer {amount} {currency} to ...",
                        )
                    ),
                )
            elif flow.step == "method_instruction":
                await create_owner_payment_method(
                    factory, actor_id=actor_id, project_id=project_id,
                    name=flow.payload["name"],
                    currency_code=flow.payload["currency"], instruction=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Способ оплаты добавлен.", "✅ Payment method added."),
                )
                await show_methods(bot, message.chat.id, actor_id, project_id)
            elif flow.step == "timezone":
                await change_project_timezone(
                    factory, project_id=project_id,
                    actor_telegram_user_id=message.from_user.id, timezone_name=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Часовой пояс обновлён.", "✅ Timezone updated."),
                )
                await show_dashboard(bot, message.chat.id, actor_id, project_id)
        except (ValueError, PermissionError, RuntimeError) as exc:
            destination = {
                "plan_name": "plans",
                "plan_duration": "plans",
                "plan_resource": "plans",
                "resource_reference": "resources",
                "method_name": "methods",
                "method_instruction": "methods",
                "method_rename": "methods",
                "method_edit": "methods",
                "timezone": "dashboard",
                "new_bot_token": "projects",
                "bot_token": "bots",
                "connection_rename": "bots",
            }.get(flow.step, "dashboard")
            if flow.step in ("method_rename", "method_edit"):
                method_hex = flow.payload.get("method_id")
                try:
                    destination = "method:" + UUID(hex=method_hex).hex
                except (TypeError, ValueError, AttributeError):
                    pass
            if flow.step in ("price_amount", "plan_rename"):
                plan_hex = flow.payload.get("plan_id")
                try:
                    destination = "plan:" + UUID(hex=plan_hex).hex
                except (TypeError, ValueError, AttributeError):
                    destination = "plans"
            await bot.send_message(
                message.chat.id,
                "⚠️ " + owner_error(str(exc))[:220]
                + tr("\nИсправьте данные и повторите действие.",
                     "\nCorrect the input and try again."),
                reply_markup=_markup([[_button(
                    tr("⬅️ Вернуться в раздел", "⬅️ Back to section"),
                    destination,
                )]]),
            )

    @router.message(StrayBotTokenFilter())
    async def stray_bot_token_handler(message: Message, bot: Bot) -> None:
        # An expired/replaced prompt must never leave a credential in chat.
        try:
            removed = await bot.delete_message(
                message.chat.id, message.message_id,
            )
        except AiogramError:
            removed = False
        if not removed:
            await bot.send_message(
                message.chat.id,
                tr("⚠️ Не удалось удалить токен. Удалите сообщение и "
                   "смените токен в @BotFather.",
                   "⚠️ Could not delete the token. Delete the message and "
                   "rotate your token in @BotFather."),
            )
            return
        await bot.send_message(
            message.chat.id,
            tr("🔐 Токен удалён из чата. Для подключения "
               "откройте @vimipay_bot → Подключить бота.",
               "🔐 Token removed from this chat. "
               "To connect your bot, open @vimipay_bot → Connect bot."),
        )

    return router
