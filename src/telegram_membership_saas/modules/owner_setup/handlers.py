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
from telegram_membership_saas.localization import recipient_language, tr
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.deletion_service import (
    request_bot_retirement,
)
from telegram_membership_saas.modules.bots.service import (
    TOKEN_PATTERN,
    decrypt_token,
    register_customer_bot,
)
from telegram_membership_saas.modules.owner_setup.obligation_locale import (
    obligation_error,
    obligation_message,
    obligation_status,
)
from telegram_membership_saas.modules.owner_setup.labels import (
    owner_button_label,
    owner_runtime_label,
)
from telegram_membership_saas.modules.owner_setup.obligations import (
    close_owner_payment,
    end_owner_subscription,
    list_owner_obligations,
    retry_owner_access,
    revoke_owner_grant,
)
from telegram_membership_saas.modules.owner_setup.service import (
    ALLOWED_CURRENCIES,
    SetupFlow,
    advance_flow,
    bind_prompt,
    can_manage_project,
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
    change_owner_method_status,
    get_owner_method_info,
    get_owner_plan_info,
    list_owner_methods,
)
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
                [_button("👀 Смотреть гостем", "preview")],
                [_button("❔ Помощь", "help")],
                [InlineKeyboardButton(text="🌐 Language / Язык", callback_data="lng:menu")],
            ])
        await bot.send_message(
            user_id, text, parse_mode="HTML", reply_markup=_markup(rows),
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
            user_id, text, parse_mode="HTML",
            reply_markup=ForceReply(
                selective=True, input_field_placeholder=tr("Введите ответ", "Type your reply")
            ),
        )
        await bind_prompt(
            factory, actor_id=actor_id, step=step,
            message_id=sent.message_id, generation=generation,
            bot_scope_id=bot.id if fixed_project_id else 0,
        )

    async def show_bots(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        lines = [tr("🤖 <b>Мои боты</b>", "🤖 <b>My bots</b>"), ""]
        if not data.bots:
            lines.append(tr("Бот пока не подключён.", "No bot connected yet."))
        for username, status in data.bots:
            lines.append(
                f"• <b>@{escape(username)}</b> — "
                + (tr("токен проверен", "token verified") if status == "registered"
                   else _status(status))
            )
        retiring = any(status == "deleting" for _, _, status in data.bot_instances)
        lines.append(
            "\n" + (
                tr("⏳ Бот отключается. Нажмите «Обновить статус» после завершения.",
                    "⏳ Bot is shutting down. Refresh its status once complete.")
                if retiring else escape(owner_runtime_label(data.bot_runtime_label))
                + tr("\n\nБот доступен для настройки сразу после подключения."
                     "\nОткройте его и добавьте каналы, оплату и тарифы.",
                     "\n\nA verified bot can be configured immediately."
                     "\nOpen it to add channels, payments and plans.")
            )
        )
        rows = []
        connected = next(
            (username for _, username, status in data.bot_instances
             if status == "registered"), None,
        )
        if connected:
            rows.append([
                InlineKeyboardButton(
                    text=tr("🚀 Перейти к своему боту", "🚀 Open my bot"),
                    url="https://t.me/" + connected,
                    style="success",
                )
            ])
        rows.append([_button("🔄 Обновить статус", "bots")])
        if not retiring:
            rows.append([_button("➕ Подключить / обновить токен", "connect")])
        if data.can_delete_bot:
            for instance_id, username, status in data.bot_instances:
                if status != "deleting":
                    rows.append([
                        _button(
                            tr("🗑 Удалить @", "🗑 Remove @") + username[:35],
                            "drop:" + encode_uuid(project_id)
                            + ":" + encode_uuid(instance_id),
                            "danger",
                        )
                    ])
        rows.extend([
            [_button("➕ Подключить другого бота", "new")],
            [_button("⬅️ Мои подключения", "dashboard")],
        ])
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
        if data.status == "draft":
            rows.append([
                _button("✅ Опубликовать тариф", "publish:" + plan_id.hex, "success"),
            ])
        rows.append([_button("⬅️ Все тарифы", "plans")])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(rows),
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
        rows = [[_button("🌐 Добавить валюту", "methodlink:" + method_id.hex)]]
        if method.status == "active":
            rows.append([_button(
                "⏸️ Отключить для новых покупок",
                "methodconfirmoff:" + method_id.hex, "danger",
            )])
        else:
            rows.append([_button(
                "▶️ Включить способ оплаты",
                "methodon:" + method_id.hex, "success",
            )])
        rows.append([_button("⬅️ Все способы оплаты", "methods")])
        await bot.send_message(
            user_id,
            "💳 <b>" + escape(method.name) + "</b>\n"
            + tr("Статус: ", "Status: ") + _status(method.status)
            + tr("\nВалюты: ", "\nCurrencies: ")
            + escape(", ".join(method.currencies) or tr("не выбраны", "none selected"))
            + tr("\n\nПокупателям показываются только способы, "
                 "совместимые с валютой тарифа.",
                 "\n\nBuyers see only payment methods that support "
                 "the selected plan currency."),
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
    ) -> None:
        """Preview active offers without a purchase or financial side effect."""
        data = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        published = [name for _, name, status in data.plans if status == "active"]
        content = (
            tr("👀 <b>Гостевой просмотр</b>\n\n",
               "👀 <b>Buyer preview</b>\n\n")
            + ("\n".join("• " + escape(name) for name in published[:15])
               if published else tr("Пока нет опубликованных тарифов.",
                                    "There are no published plans yet."))
            + tr("\n\nЭто просмотр каталога. Никакие платежи не создаются.",
                 "\n\nThis is a catalog preview. No payment is created.")
        )
        await bot.send_message(
            user_id, content, parse_mode="HTML",
            reply_markup=_markup([
                [_button("⬅️ Панель администратора", "dashboard")],
            ]),
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
        rows.append([_button("⬅️ Панель управления", "dashboard")])
        await bot.send_message(
            user_id, tr("💱 Выберите валюту:", "💱 Choose a currency:"),
            reply_markup=_markup(rows),
        )

    @router.message(
        Command("start", "menu"), OwnerHomeFilter(factory, fixed_project_id),
    )
    async def home_message(message: Message, bot: Bot) -> None:
        if not _private(message):
            return
        actor_id = await actor(message.from_user)
        await show_home(bot, message.chat.id, actor_id)

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
                "new", "projects", "dashboard", "bots", "connect",
                "help", "oblist",
            } or action.startswith((
                "select:", "drop:", "dropok:", "ob:", "obok:", "obpage:",
            ))
            if not is_registry_action:
                await query.answer(
                    tr("Настройте продажи в своём подключённом боте",
                       "Set up sales inside your connected bot"),
                    show_alert=True,
                )
                return
        else:
            if action in {"new", "projects", "bots", "connect", "oblist"} or (
                action.startswith((
                    "select:", "drop:", "dropok:", "ob:", "obok:", "obpage:",
                ))
            ):
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
            if action == "projects":
                home = await owner_home(factory, actor_id=actor_id)
                rows = [
                    [_button("📁 " + name[:45], "select:" + pid.hex)]
                    for pid, name in home.projects[:20]
                ]
                rows.append([_button("➕ Подключить бота", "new", "success")])
                if platform_navigation(user_id):
                    rows.append(platform_navigation(user_id))
                await bot.send_message(user_id, tr("📁 Мои боты", "📁 My bots"),
                                       reply_markup=_markup(rows))
                await query.answer()
                return
            if action.startswith("select:"):
                await choose_project(
                    factory, actor_id=actor_id,
                    project_id=UUID(hex=action.split(":", 1)[1]),
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
            elif action == "reviews":
                await show_reviews(bot, user_id, actor_id, project_id)
            elif action == "preview":
                await show_guest_preview(bot, user_id, actor_id, project_id)
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
                        keyboard=[[KeyboardButton(
                            text=tr("📢 Выбрать чат", "📢 Choose chat"),
                            request_chat=KeyboardButtonRequestChat(
                                request_id=request_id,
                                chat_is_channel=(kind == "channel"),
                                request_title=True,
                            ),
                        )]],
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
                        reply_markup=_markup(rows),
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
                plan_id = UUID(hex=action.split(":", 1)[1])
                try:
                    name = await publish_owner_plan(
                        factory, actor_id=actor_id, project_id=project_id,
                        plan_id=plan_id,
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
                if not rows:
                    await bot.send_message(
                        user_id, tr("Все доступные валюты уже подключены.",
                                     "All available currencies are already enabled."),
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
            elif action.startswith("methodconfirmoff:"):
                method_id = UUID(hex=action.split(":", 1)[1])
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
                            "⏸️ Подтвердить отключение",
                            "methodoff:" + method_id.hex, "danger",
                        )],
                        [_button("Отмена", "method:" + method_id.hex)],
                    ]),
                )
            elif action.startswith(("methodoff:", "methodon:")):
                is_active = action.startswith("methodon:")
                method_id = UUID(hex=action.split(":", 1)[1])
                await change_owner_method_status(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, enabled=is_active,
                )
                await show_method_card(
                    bot, user_id, actor_id, project_id, method_id,
                )
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
                    parse_mode="HTML",
                )
            else:
                raise ValueError(tr("Неизвестное действие", "Unknown action"))
        except (ValueError, PermissionError) as exc:
            detail = (
                obligation_error(str(exc))
                if action.startswith(("ob:", "obok:", "obpage:"))
                else str(exc)
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
                message.chat.id, "⚠️ " + str(exc),
                reply_markup=ReplyKeyboardRemove(),
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
            ),
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
            "new_bot_token", "bot_token",
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
            "new_bot_token", "bot_token", "project_name",
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
            if flow.step == "bot_token":
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
                await bot.send_message(
                    message.chat.id,
                    tr("⏳ Выберите срок действия:", "⏳ Choose plan duration:"),
                    reply_markup=_markup([
                        [_button(tr(value[2], format_period(value[0], value[1])),
                                  "duration:" + key + ":" + generation)]
                        for key, value in DURATIONS.items()
                    ]),
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
            await bot.send_message(
                message.chat.id,
                "⚠️ " + str(exc)[:220]
                + tr("\nОткройте нужный раздел и повторите.",
                     "\nReopen the relevant section and try again."),
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
