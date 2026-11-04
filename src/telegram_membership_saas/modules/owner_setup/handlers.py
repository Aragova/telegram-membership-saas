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
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.deletion_service import (
    request_bot_retirement,
)
from telegram_membership_saas.modules.bots.service import (
    TOKEN_PATTERN,
    decrypt_token,
    register_customer_bot,
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
    return InlineKeyboardButton(text=label, callback_data="own:" + data, style=style)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _status(value: str) -> str:
    return {
        "active": "🟢 Активно",
        "registered": "✅ Подключён",
        "draft": "📝 Черновик",
        "disabled": "⚪ Отключено",
        "archived": "📦 Архив",
        "deleting": "🟡 Удаляется",
    }.get(value, "Неизвестно")


def _next_setup_step(snapshot) -> tuple[str, str] | None:
    """One contextual action at a time, not an error-driven wizard."""
    if not any(status == "active" for _, _, status in snapshot.resources):
        return "📢 1. Подключить канал", "resources"
    if not any(status == "active" for _, _, status in snapshot.methods):
        return "💳 2. Настроить оплату", "methods"
    if not any(status == "active" for _, _, status in snapshot.plans):
        return "📦 3. Подготовить тариф", "plans"
    if not snapshot.sales_configured:
        # A published plan can lose its last currency-compatible payment
        # method without losing its draft prices. Readiness must lead to an
        # actionable repair, never back to itself.
        return "💳 4. Проверить валюты оплаты", "methods"
    return None


def _private(message: Message) -> bool:
    return message.chat.type == "private" and message.from_user is not None


def _progress(snapshot) -> tuple[str, ...]:
    # A registered bot is configured, not necessarily running.
    steps = (
        ("Собственный бот", any(s == "registered" for _, s in snapshot.bots)),
        ("Канал / группа", any(s == "active" for _, _, s in snapshot.resources)),
        ("Тариф", any(s == "active" for _, _, s in snapshot.plans)),
        ("Способ оплаты", any(s == "active" for _, _, s in snapshot.methods)),
        ("Бот принимает покупателей", bool(snapshot.live_bot_username)),
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
                text="👑 Панель платформы", callback_data="plat:home",
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
        lines = ["✨ <b>ViMi Pay · Управление подписками</b>", ""]
        rows: list[list[InlineKeyboardButton]] = []
        if not home.projects:
            lines.append(
                "Подключите собственного Telegram-бота, затем "
                "откройте его для настройки тарифов и оплаты."
            )
        else:
            lines.append("Выберите проект для управления:")
            for project_id, name in home.projects[:20]:
                rows.append([_button("🤖 " + name[:45], "select:" + project_id.hex)])
        rows.append([_button("➕ Подключить бота подписок", "new", "success")])
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
                "✦ <b>ViMi Pay</b>\n"
                "<i>Ваши подключения</i>\n\n"
                f"🤖 <b>{escape(data.project_name)}</b>\n"
                f"{escape(data.bot_runtime_label)}\n\n"
                "Подключите бота и продолжите настройку внутри него."
            )
            rows = [
                [_button("🤖 Управление ботом", "bots", "success")],
                [_button("📁 Все подключения", "projects")],
                [_button("❔ Как это работает", "help")],
            ]
            if platform_navigation(user_id):
                rows.append(platform_navigation(user_id))
        else:
            next_step = _next_setup_step(data)
            text = (
                "✦ <b>Панель администратора</b>\n"
                f"<b>{escape(data.project_name)}</b>\n"
                f"🌍 {escape(data.timezone)}\n\n"
                + "\n".join(_progress(data))
                + "\n\n"
                + (
                    "✅ Продажи готовы. Управляйте тарифами и заявками."
                    if data.sales_configured
                    else "Настройте продажи по порядку. Уже сохранённые "
                    "данные останутся на месте."
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
                selective=True, input_field_placeholder="Введите ответ"
            ),
        )
        await bind_prompt(
            factory, actor_id=actor_id, step=step,
            message_id=sent.message_id, generation=generation,
            bot_scope_id=bot.id if fixed_project_id else 0,
        )

    async def show_bots(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        lines = ["🤖 <b>Мои боты</b>", ""]
        if not data.bots:
            lines.append("Бот пока не подключён.")
        for username, status in data.bots:
            lines.append(
                f"• <b>@{escape(username)}</b> — "
                + ("токен проверен" if status == "registered" else _status(status))
            )
        retiring = any(status == "deleting" for _, _, status in data.bot_instances)
        lines.append(
            "\n" + (
                "⏳ Бот отключается. Нажмите «Обновить статус» после завершения."
                if retiring else escape(data.bot_runtime_label)
                + "\n\nБот доступен для настройки сразу после подключения."
                "\nОткройте его и добавьте каналы, оплату и тарифы."
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
                    text="🚀 Перейти к своему боту",
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
                            "🗑 Удалить @" + username[:35],
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
            "🧾 <b>Обязательства перед отключением</b>",
            "Страница " + str(page + 1) + " · до 5 записей каждого типа",
            "",
            "Заявки: " + str(counts.requests)
            + " · Подписки: " + str(counts.subscriptions)
            + " · Права: " + str(counts.grants),
            "Доступы: " + str(counts.access)
            + " · Действия: " + str(counts.access_actions)
            + " · Приглашения: " + str(counts.join_tickets),
            "",
            "Все действия выполняются отдельно с подтверждением. "
            "Оплаченный доступ нельзя отключать без урегулирования "
            "обязательств перед покупателем.",
        ]
        rows: list[list[InlineKeyboardButton]] = []
        project_ref = encode_uuid(project_id)
        for item in snapshot.payments:
            label = (
                "Отклонить" if item.status == "waiting_review" else "Закрыть"
            )
            lines.append(
                "💳 " + escape(item.username or str(item.telegram_user_id))
                + " · " + escape(item.amount + " " + item.currency)
                + " · " + escape(item.status)
            )
            rows.append([_button(
                "💳 " + label + " заявку " + str(item.telegram_user_id),
                "ob:pay:" + project_ref + ":" + encode_uuid(item.id)
                + (":" + str(page) if page else ""), "danger",
            )])
        for item in snapshot.subscriptions:
            end = (
                item.period_end.strftime("%d.%m.%Y") if item.period_end else "бессрочно"
            )
            lines.append(
                "📦 " + escape(item.plan_name) + " · "
                + escape(item.username or str(item.telegram_user_id))
                + " · до " + end
            )
            rows.append([_button(
                "📦 Завершить подписку " + str(item.telegram_user_id),
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
                "🔑 Отозвать " + item.source[:12] + " · " + str(item.telegram_user_id),
                "ob:grant:" + project_ref + ":" + encode_uuid(item.id)
                + (":" + str(page) if page else ""), "danger",
            )])
        if snapshot.access:
            lines.append(
                "🔄 Доступы требуют подтверждения фактического отзыва в Telegram. "
                "Они не очищаются принудительно."
            )
            for item in snapshot.access[:12]:
                rows.append([_button(
                    "🔄 Синхронизировать доступ · " + item.actual,
                    "ob:sync:" + project_ref + ":" + encode_uuid(item.id)
                    + (":" + str(page) if page else ""),
                )])
        if counts.access_actions or counts.join_tickets:
            lines.append(
                "⏳ Действия доступа и приглашения обрабатывает клиентский бот; "
                "проверьте состояние после завершения."
            )
        if not counts.has_blockers:
            lines.append("\n✅ Блокировок больше нет. Можно вернуться к удалению.")
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
                owner_tg_id, "⚠️ Состояние сохранено, но уведомление покупателя недоступно.",
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
                owner_tg_id, "⚠️ Изменение сохранено, но бот покупателя отключён.",
            )
            return
        try:
            customer_bot = Bot(token=decrypt_token(cipher, key=key))
            try:
                await customer_bot.send_message(
                    change.telegram_user_id, change.message,
                )
            finally:
                await customer_bot.session.close()
        except (AiogramError, ValueError, RuntimeError, InvalidToken):
            await central_bot.send_message(
                owner_tg_id,
                "⚠️ Изменение сохранено, но Telegram не доставил уведомление покупателю.",
            )

    async def show_resources(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        lines = ["📢 <b>Каналы и группы</b>", ""]
        lines.extend(
            f"• {escape(name)} — {_status(status)}"
            for _, name, status in data.resources
        )
        if not data.resources:
            lines.append("Нет подключённых каналов и групп.")
        lines.append("\nСначала добавьте своего бота администратором канала.")
        await bot.send_message(user_id, "\n".join(lines), parse_mode="HTML",
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
        lines = ["📦 <b>Тарифы</b>", ""]
        if not data.plans:
            lines.append("Тарифов пока нет.")
        for pid, name, status in data.plans[:15]:
            lines.append(f"• <b>{escape(name)}</b> — {_status(status)}")
            rows.append([_button("⚙️ " + name[:45], "plan:" + pid.hex)])
        rows.extend([
            [_button("➕ Создать тариф", "newplan", "success")],
        ])
        if not data.methods:
            lines.append("\n💳 Настройте оплату, затем публикуйте тарифы.")
            rows.append([_button("➕ Настроить оплату", "methods")])
        rows.append([_button("⬅️ Панель администратора", "dashboard")])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML", reply_markup=_markup(rows)
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
            "Статус: " + _status(data.status),
            "⏳ " + escape(format_period(data.duration_value, data.duration_unit)),
            "📢 " + escape(data.resource_name or "Канал не привязан"),
            "",
            "<b>Цены и оплата</b>",
        ]
        if not data.prices:
            lines.append("Цена ещё не указана.")
        for price in data.prices:
            lines.append(
                ("✅ " if price.method_count else "⚠️ ")
                + escape(
                    format_amount(price.amount, price.precision)
                    + " " + price.currency
                )
                + (
                    " · оплата настроена"
                    if price.method_count
                    else " · нужен способ оплаты"
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
            "💳 <b>Способы оплаты</b>",
            "Страница " + str(page + 1),
            "",
        ]
        rows = []
        if not methods:
            lines.append(
                "Нет способов оплаты на этой странице."
                if page else
                "Пока ничего не подключено. Для начала выберите валюту "
                "и добавьте способ перевода."
            )
        for method in methods:
            lines.append(
                "• <b>" + escape(method.name) + "</b> — "
                + _status(method.status) + "\n  "
                + escape(", ".join(method.currencies) or "Без валюты")
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
        rows.append([
            _button("➕ Добавить способ", "newmethod", "success"),
        ])
        if methods or page > 0:
            rows.append([
                _button("➡️ Перейти к тарифам", "plans"),
            ])
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
        rows = [[_button(
            "🌐 Добавить валюту", "methodlink:" + method_id.hex,
        )]]
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
            "Статус: " + _status(method.status)
            + "\nВалюты: "
            + escape(", ".join(method.currencies) or "не выбраны")
            + "\n\nПокупателям показываются только способы, "
            "совместимые с валютой тарифа.",
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
                "📋 <b>Заявки</b>\n\n"
                "Сейчас нет платежей, ожидающих проверки.\n"
                "Новые подтверждения придут сюда вместе с чеком.",
                parse_mode="HTML",
                reply_markup=_markup([
                    [_button("⬅️ Панель администратора", "dashboard")],
                ]),
            )
            return
        await bot.send_message(
            user_id,
            f"📋 <b>Заявки на проверку — {len(items)}</b>\n\n"
            "Перед подтверждением убедитесь, что деньги "
            "действительно поступили в банк.",
            parse_mode="HTML",
        )
        for item in items:
            await bot.send_message(
                user_id, _review_text(item),
                reply_markup=_review_keyboard(item.request_id),
            )
        await bot.send_message(
            user_id, "Все доступные заявки показаны выше.",
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
            "👀 <b>Гостевой просмотр</b>\n\n"
            + ("\n".join("• " + escape(name) for name in published[:15])
               if published else "Пока нет опубликованных тарифов.")
            + "\n\nЭто просмотр каталога. Никакие платежи не создаются."
        )
        await bot.send_message(
            user_id, content, parse_mode="HTML",
            reply_markup=_markup([
                [_button("⬅️ Панель администратора", "dashboard")],
            ]),
        )

    async def currency_picker(bot: Bot, user_id: int, actor_id: UUID, *,
                              mode: str, plan_id: UUID | None = None):
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
            user_id, "💱 Выберите валюту:", reply_markup=_markup(rows),
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
            await query.answer("Откройте личный чат с ViMi Pay", show_alert=True)
            return
        user_id = query.from_user.id
        actor_id = await actor(query.from_user)
        action = (query.data or "")[4:]
        if fixed_project_id is not None and not await can_manage_project(
            factory, actor_id=actor_id, project_id=fixed_project_id,
        ):
            await query.answer("Нет доступа к управлению ботом", show_alert=True)
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
                    "Настройте продажи в своём подключённом боте",
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
                    "Управляйте подключением через @vimipay_bot",
                    show_alert=True,
                )
                return
        try:
            if action == "new":
                if get_settings().bot_token_encryption_key is None:
                    raise ValueError("Подключение ботов пока недоступно")
                await prompt(
                    bot, user_id, actor_id, step="new_bot_token",
                    text=(
                        "✦ <b>Подключение бота подписок</b>\n\n"
                        "1. Создайте бота в @BotFather или получите новый "
                        "токен существующего.\n"
                        "2. Ответьте на это сообщение токеном.\n"
                        "3. Мы сразу удалим сообщение и проверим подключение.\n\n"
                        "После проверки откройте своего бота и "
                        "настройте каналы, оплату и тарифы."
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
                await bot.send_message(user_id, "📁 Мои боты", reply_markup=_markup(rows))
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
                raise ValueError("Сначала создайте или выберите проект")
            if action == "dashboard":
                await show_dashboard(bot, user_id, actor_id, project_id)
            elif action == "reviews":
                await show_reviews(bot, user_id, actor_id, project_id)
            elif action == "preview":
                await show_guest_preview(bot, user_id, actor_id, project_id)
            elif action.startswith("drop:"):
                _, project_ref, instance_ref = action.split(":", 2)
                if decode_uuid(project_ref) != project_id:
                    raise ValueError("Меню другого проекта. Откройте «Мои боты» снова.")
                instance_id = decode_uuid(instance_ref)
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id
                )
                name = next((
                    username for bot_id, username, status in data.bot_instances
                    if bot_id == instance_id and status != "deleting"
                ), None)
                if name is None or not data.can_delete_bot:
                    raise PermissionError("Этот бот не доступен для удаления")
                await bot.send_message(
                    user_id,
                    "🗑 <b>Удалить @" + escape(name) + "?</b>\n\n"
                    "ViMi Pay отключит обработку покупателей, затем удалит "
                    "подключение, сохранённый токен и состояние запуска.\n\n"
                    "Настройки проекта и история платежей останутся: они "
                    "не принадлежат токену бота. Повторно подключить этот "
                    "Telegram-бот можно после завершения удаления.\n\n"
                    "При незавершённых оплатах или действующих подписках "
                    "удаление не начнётся, чтобы не оставить людей без "
                    "управления доступом.",
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
                    raise ValueError("Проект изменился. Откройте «Мои боты» снова.")
                try:
                    requested = await request_bot_retirement(
                        factory, actor_id=actor_id,
                        project_id=project_id,
                        bot_instance_id=decode_uuid(instance_ref),
                    )
                except ValueError as exc:
                    # Include the entire safety explanation; Telegram alert
                    # callbacks are limited in length and would truncate it.
                    await bot.send_message(user_id, "⚠️ " + str(exc))
                    await show_obligations(bot, user_id, actor_id, project_id)
                    await query.answer("Удаление пока недоступно", show_alert=True)
                    return
                await bot.send_message(
                    user_id,
                    "✅ Удаление запрошено. Бот прекратит обработку покупателей, "
                    "а после безопасной остановки ViMi Pay удалит его "
                    "данные подключения.\n\n"
                    "Нажмите «🔄 Обновить статус» в разделе «Мои боты». "
                    "Когда бот исчезнет из списка, его можно подключить "
                    "заново или создать новый проект с нуля."
                    if requested else
                    "Этот бот уже не подключён. Обновите список.",
                )
                await show_bots(bot, user_id, actor_id, project_id)
            elif action == "oblist":
                await show_obligations(bot, user_id, actor_id, project_id)
            elif action.startswith("obpage:"):
                _, project_ref, page_number = action.split(":", 2)
                if decode_uuid(project_ref) != project_id:
                    raise ValueError("Открыто меню другого проекта")
                await show_obligations(
                    bot, user_id, actor_id, project_id, page=int(page_number),
                )
            elif action.startswith(("ob:", "obok:")):
                parts = action.split(":")
                if len(parts) not in (4, 5) or parts[1] not in (
                    "pay", "sub", "grant", "sync"
                ):
                    raise ValueError("Неизвестное действие")
                verb, kind, project_ref, object_ref = parts[:4]
                page = int(parts[4]) if len(parts) == 5 else 0
                if decode_uuid(project_ref) != project_id:
                    raise ValueError("Открыто меню другого проекта. Обновите обязательства.")
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
                        raise ValueError("Действие уже неактуально. Обновите обязательства.")
                    warnings = {
                        "pay": "Проверьте поступление денег в банке. "
                        "Отклонение или отмена заявки не возвращает деньги.",
                        "sub": "Вы досрочно закрываете оплаченный срок! "
                        "Возврат денег ViMi Pay не выполняет. "
                        "Урегулируйте вопрос с покупателем самостоятельно.",
                        "grant": "Это отдельное право доступа. "
                        "Проверьте, не является ли оно служебным или бессрочным.",
                        "sync": "ViMi Pay перепроверит права и отправит действие "
                        "в Telegram. Доступ не будет удалён при других действующих правах.",
                    }
                    await bot.send_message(
                        user_id, "⚠️ <b>Подтвердите действие</b>\n\n"
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
                        raise ValueError("Действие уже выполнено или устарело")
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
                        await bot.send_message(user_id, "🔄 " + message)
                        change = None
                    if change is not None:
                        await bot.send_message(
                            user_id, "✅ " + (
                                "Состояние сохранено" if change.changed
                                else change.message
                            ),
                        )
                        await notify_buyer(bot, user_id, actor_id, project_id, change)
                    await show_obligations(bot, user_id, actor_id, project_id)
            elif action == "bots":
                await show_bots(bot, user_id, actor_id, project_id)
            elif action == "connect":
                if get_settings().bot_token_encryption_key is None:
                    raise ValueError("Подключение ботов временно недоступно")
                await prompt(
                    bot, user_id, actor_id, step="bot_token",
                    text=(
                        "🤖 <b>Подключение вашего бота</b>\n\n"
                        "1. Создайте бота в @BotFather.\n"
                        "2. Ответьте на это сообщение токеном.\n"
                        "3. ViMi Pay сразу удалит ваше сообщение и проверит токен.\n\n"
                        "Бот пока не начнёт принимать покупателей."
                    ),
                )
            elif action == "resources":
                await show_resources(bot, user_id, actor_id, project_id)
            elif action.startswith("pick:"):
                kind = action.split(":", 1)[1]
                if kind not in ("channel", "group"):
                    raise ValueError("Неизвестный тип чата")
                request_id = randbelow(2**31 - 2) + 1
                await set_flow(
                    factory, actor_id=actor_id,
                    step="chat_picker", payload={"request_id": request_id},
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                await bot.send_message(
                    user_id,
                    "📢 Выберите ваш канал или группу с помощью кнопки ниже.\n"
                    "Убедитесь, что ваш подключённый бот уже добавлен "
                    "администратором с правами приглашения и блокировки.",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[[KeyboardButton(
                            text="📢 Выбрать чат",
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
                    text="📢 Отправьте ID канала (например, -100…) или @username.\n"
                    "Ваш собственный бот уже должен быть администратором.",
                )
            elif action == "plans":
                await show_plans(bot, user_id, actor_id, project_id)
            elif action == "newplan":
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id
                )
                if not any(status == "active" for _, _, status in data.resources):
                    raise ValueError("Сначала подключите действующий канал или группу")
                await prompt(
                    bot, user_id, actor_id, step="plan_name",
                    text="📦 Как назвать новый тариф?\nНапример: Premium",
                )
            elif action.startswith("duration:"):
                _, key, generation = action.split(":", 2)
                if key not in DURATIONS:
                    raise ValueError("Неподдерживаемый срок")
                flow = await consume_button_flow(
                    factory, actor_id=actor_id,
                    expected_step="plan_duration", generation=generation,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                if flow.project_id != project_id:
                    raise ValueError("Проект изменился. Повторите действие.")
                value, unit, _ = DURATIONS[key]
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id
                )
                resources = [
                    rid for rid, _, status in data.resources if status == "active"
                ]
                if not resources:
                    raise ValueError("Сначала подключите канал")
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
                        user_id, "📢 Какой канал откроет этот тариф?",
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
                    raise ValueError("Проект изменился. Повторите действие.")
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
                    raise ValueError("Неизвестная валюта")
                await prompt(
                    bot, user_id, actor_id, step="price_amount",
                    payload={"plan_id": plan_hex, "currency": currency},
                    text=f"💰 Укажите цену в {currency} (например, 499 или 9.99):",
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
                        "💳 <b>Остался один шаг</b>\n\n"
                        "Добавьте способ оплаты, который поддерживает "
                        "валюту тарифа. Черновик и цены сохранены.",
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
                    user_id, f"✅ Тариф «{escape(name)}» опубликован.",
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
                        user_id, "Все доступные валюты уже подключены.",
                    )
                else:
                    rows.append([
                        _button("⬅️ Вернуться", "method:" + method_id.hex),
                    ])
                    await bot.send_message(
                        user_id,
                        "🌐 <b>" + escape(method.name) + "</b>\n\n"
                        "Выберите дополнительную валюту. "
                        "Текущие подключения сохранятся.",
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
                    "✅ Валюта подключена" if added
                    else "Эта валюта уже подключена.",
                )
                await show_method_card(
                    bot, user_id, actor_id, project_id, UUID(hex=method_hex),
                )
            elif action.startswith("methodconfirmoff:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                await bot.send_message(
                    user_id,
                    "⏸️ <b>Отключить способ оплаты?</b>\n\n"
                    "Он исчезнет из новых покупок. Уже полученные "
                    "заявки и история платежей сохранятся.",
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
                    raise ValueError("Неизвестная валюта")
                await prompt(
                    bot, user_id, actor_id, step="method_name",
                    payload={"currency": code},
                    text=f"💳 Название способа оплаты для {code}:\n"
                    "Например: СБП или Банковский перевод.",
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
                    user_id, "🌍 Выберите часовой пояс:",
                    reply_markup=_markup(rows),
                )
            elif action == "tzcustom":
                await prompt(
                    bot, user_id, actor_id, step="timezone",
                    text="🌍 Отправьте часовой пояс IANA, например Europe/Moscow.",
                )
            elif action.startswith("tz:"):
                _, value, origin_hex = action.split(":", 2)
                if UUID(hex=origin_hex) != project_id:
                    raise ValueError("Это меню другого проекта. Откройте настройки заново.")
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
                    "📋 <b>Готовность к продажам</b>\n\n"
                    + "\n".join(_progress(data))
                    + "\n\n🤖 " + escape(data.bot_runtime_label)
                    + "\n\n"
                    + (
                        "✅ Тарифы доступны для покупки."
                        if data.sales_configured
                        else "Бот уже может принимать настройки. "
                        "Покупатели увидят тарифы после завершения шагов."
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup(rows),
                )
            elif action == "help":
                await bot.send_message(
                    user_id,
                    "❔ <b>Как запустить подписки</b>\n\n"
                    "1. Подключите токен в @vimipay_bot.\n"
                    "2. Откройте собственный бот и добавьте его в канал.\n"
                    "3. Настройте валюты и способы оплаты.\n"
                    "4. Создайте тариф, цену и опубликуйте.\n\n"
                    "Ваши чеки ViMi Pay не хранит — поступления "
                    "проверяются администратором в банке.",
                    parse_mode="HTML",
                )
            else:
                raise ValueError("Неизвестное действие")
        except (ValueError, PermissionError) as exc:
            await query.answer(str(exc)[:180], show_alert=True)
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
                raise ValueError("Настройка другого бота недоступна")
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
            "✅ Канал «" + escape(chat.name) + "» подключён.",
            parse_mode="HTML", reply_markup=ReplyKeyboardRemove(),
        )
        await show_resources(bot, message.chat.id, actor_id, flow.project_id)

    async def send_connected_bot(
        bot: Bot, user_id: int, actor_id: UUID,
        project_id: UUID, username: str,
    ) -> None:
        await bot.send_message(
            user_id,
            f"✅ <b>@{escape(username)} подключён</b>\n\n"
            "Настройка в ViMi Pay завершена.\n"
            "Перейдите в свой бот и добавьте канал, "
            "способы оплаты и тарифы.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🚀 Перейти к своему боту",
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
                    "⚠️ Не удалось удалить токен. Смените его в BotFather.",
                )
                return
            if unexpected_secret:
                await bot.send_message(
                    message.chat.id,
                    "⚠️ Вы прислали токен вместо запрошенных данных. "
                    "Для безопасности смените токен в BotFather и повторите шаг.",
                )
                return
        # Old central setup prompts remain in Telegram after a deployment.
        # Do not permit completing tenant settings via stale central messages.
        if fixed_project_id is None and flow.step not in (
            "new_bot_token", "bot_token",
        ):
            await bot.send_message(
                message.chat.id,
                "Настройка перенесена в вашего бота. "
                "Откройте его через «Мои боты».",
            )
            return
        if fixed_project_id is not None and flow.step in (
            "new_bot_token", "bot_token", "project_name",
        ):
            await bot.send_message(
                message.chat.id,
                "Подключение токена доступно только в @vimipay_bot.",
            )
            return
        try:
            if flow.step == "new_bot_token":
                if not TOKEN_PATTERN.fullmatch(text):
                    raise ValueError(
                        "Неверный формат токена. Получите токен в @BotFather."
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
                    "✅ Проект создан. Часовой пояс — Москва (можно изменить).",
                )
                await show_home(bot, message.chat.id, actor_id)
                return
            project_id = flow.project_id
            if project_id is None or (
                fixed_project_id is not None and project_id != fixed_project_id
            ):
                raise ValueError("Настройка другого бота недоступна")
            if flow.step == "bot_token":
                if not TOKEN_PATTERN.fullmatch(text):
                    raise ValueError("Неверный формат токена. Получите его в BotFather.")
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
                    f"✅ Канал «{escape(resource.name)}» подключён.",
                    parse_mode="HTML",
                )
                await show_resources(bot, message.chat.id, actor_id, project_id)
            elif flow.step == "plan_name":
                if not 2 <= len(text) <= 120:
                    raise ValueError("Название тарифа: от 2 до 120 символов")
                generation = await advance_flow(
                    factory, actor_id=actor_id, source=flow,
                    step="plan_duration", payload={"name": text},
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                await bot.send_message(
                    message.chat.id,
                    "⏳ Выберите срок действия:",
                    reply_markup=_markup([
                        [_button(value[2], "duration:" + key + ":" + generation)]
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
                await bot.send_message(message.chat.id, "✅ Цена сохранена.")
                await show_plan_card(bot, message.chat.id, actor_id, project_id, plan_id)
            elif flow.step == "method_name":
                if not 2 <= len(text) <= 120:
                    raise ValueError("Название способа оплаты: от 2 до 120 символов")
                await prompt(
                    bot, message.chat.id, actor_id,
                    step="method_instruction",
                    source_flow=flow,
                    payload={"currency": flow.payload["currency"], "name": text},
                    text=(
                        "💳 Отправьте инструкцию покупателю: куда и как переводить "
                        "деньги.\n\nМожно использовать {amount}, {currency}, "
                        "{plan}, {period}.\n"
                        "Например: Переведите {amount} {currency} по СБП на ..."
                    ),
                )
            elif flow.step == "method_instruction":
                await create_owner_payment_method(
                    factory, actor_id=actor_id, project_id=project_id,
                    name=flow.payload["name"],
                    currency_code=flow.payload["currency"], instruction=text,
                )
                await bot.send_message(message.chat.id, "✅ Способ оплаты добавлен.")
                await show_methods(bot, message.chat.id, actor_id, project_id)
            elif flow.step == "timezone":
                await change_project_timezone(
                    factory, project_id=project_id,
                    actor_telegram_user_id=message.from_user.id, timezone_name=text,
                )
                await bot.send_message(message.chat.id, "✅ Часовой пояс обновлён.")
                await show_dashboard(bot, message.chat.id, actor_id, project_id)
        except (ValueError, PermissionError, RuntimeError) as exc:
            await bot.send_message(
                message.chat.id,
                "⚠️ " + str(exc)[:220] + "\nОткройте нужный раздел и повторите.",
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
                "⚠️ Не удалось удалить токен. Удалите сообщение и "
                "смените токен в @BotFather.",
            )
            return
        await bot.send_message(
            message.chat.id,
            "🔐 Токен удалён из чата. Для подключения "
            "откройте @vimipay_bot → Подключить бота.",
        )

    return router
