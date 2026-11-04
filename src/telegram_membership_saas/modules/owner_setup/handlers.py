"""End-to-end owner setup UI for central ViMi Pay (not customer storefront)."""

from __future__ import annotations

from html import escape
from secrets import randbelow
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import BaseFilter, Command
from cryptography.fernet import InvalidToken
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.db import tenant_session_scope
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
    save_owner_price,
    set_flow,
    setup_overview,
    verify_channel_and_add,
)
from telegram_membership_saas.modules.telegram_admin.service import change_project_timezone
from telegram_membership_saas.modules.telegram_commerce.service import (
    decode_uuid,
    encode_uuid,
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


class SetupReplyFilter(BaseFilter):
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

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
        return await is_expected_prompt(
            self.factory, actor_id=actor_id,
            reply_message_id=origin.message_id,
        )


def create_owner_setup_router(
    factory: async_sessionmaker[AsyncSession],
) -> Router:
    router = Router(name="owner-setup")

    async def actor(user) -> UUID:
        return await ensure_telegram_actor(
            factory, telegram_id=user.id,
            username=user.username, first_name=user.first_name,
        )

    async def show_home(bot: Bot, user_id: int, actor_id: UUID) -> None:
        home = await owner_home(factory, actor_id=actor_id)
        if home.selected is not None:
            await show_dashboard(bot, user_id, actor_id, home.selected)
            return
        lines = ["✨ <b>ViMi Pay · Управление подписками</b>", ""]
        rows: list[list[InlineKeyboardButton]] = []
        if not home.projects:
            lines.append(
                "Создайте первый проект: подключите собственный бот, "
                "канал, тарифы и способы оплаты."
            )
        else:
            lines.append("Выберите проект для управления:")
            for project_id, name in home.projects[:20]:
                rows.append([_button("📁 " + name[:45], "select:" + project_id.hex)])
        rows.append([_button("➕ Создать проект", "new", "success")])
        await bot.send_message(
            user_id, "\n".join(lines),
            parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_dashboard(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        data = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id
        )
        text = (
            "✨ <b>ViMi Pay · Панель владельца</b>\n\n"
            f"📁 <b>{escape(data.project_name)}</b>\n"
            f"🌍 {escape(data.timezone)}\n\n"
            + "\n".join(_progress(data))
            + "\n\n🤖 " + escape(data.bot_runtime_label)
            + (
                "\n\n✅ Проект настроен. Управляйте ботом и подписками."
                if data.live_bot_username else
                "\n\nПродолжайте настройку проекта через меню."
            )
        )
        rows = [
            [_button("🤖 Мой бот", "bots"), _button("📢 Каналы", "resources")],
            [_button("📦 Тарифы", "plans"), _button("💳 Оплата", "methods")],
            [_button("🌍 Часовой пояс", "timezone")],
            [_button("📋 Проверить готовность", "readiness")],
            [_button("📁 Мои проекты", "projects"), _button("❔ Помощь", "help")],
        ]
        await bot.send_message(
            user_id, text, parse_mode="HTML", reply_markup=_markup(rows)
        )

    async def prompt(
        bot: Bot, user_id: int, actor_id: UUID,
        *, step: str, text: str, payload: dict | None = None,
        source_flow: SetupFlow | None = None,
    ) -> None:
        if source_flow is None:
            generation = await set_flow(
                factory, actor_id=actor_id, step=step, payload=payload
            )
        else:
            generation = await advance_flow(
                factory, actor_id=actor_id, source=source_flow,
                step=step, payload=payload or {},
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
                + "\n\nПосле настройки канала, опубликованного тарифа "
                "и оплаты система запускает вашего бота автоматически."
            )
        )
        rows = []
        if data.live_bot_username:
            rows.append([
                InlineKeyboardButton(
                    text="🟢 Открыть моего бота",
                    url="https://t.me/" + data.live_bot_username,
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
            [_button("➕ Новый проект с нуля", "new")],
            [_button("⬅️ Панель управления", "dashboard")],
        ])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(rows),
        )


    async def show_obligations(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        snapshot = await list_owner_obligations(
            factory, actor_id=actor_id, project_id=project_id,
        )
        counts = snapshot.blockers
        lines = [
            "🧾 <b>Обязательства перед отключением</b>",
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
                "ob:pay:" + project_ref + ":" + encode_uuid(item.id), "danger",
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
                "ob:sub:" + project_ref + ":" + encode_uuid(item.id), "danger",
            )])
        for item in snapshot.grants:
            if item.source == "payment":
                continue
            lines.append(
                "🔑 " + escape(item.source) + " · " + str(item.telegram_user_id)
            )
            rows.append([_button(
                "🔑 Отозвать " + item.source[:12] + " · " + str(item.telegram_user_id),
                "ob:grant:" + project_ref + ":" + encode_uuid(item.id), "danger",
            )])
        if snapshot.access:
            lines.append(
                "🔄 Доступы требуют подтверждения фактического отзыва в Telegram. "
                "Они не очищаются принудительно."
            )
            for item in snapshot.access[:12]:
                rows.append([_button(
                    "🔄 Синхронизировать доступ · " + item.actual,
                    "ob:sync:" + project_ref + ":" + encode_uuid(item.id),
                )])
        if counts.access_actions or counts.join_tickets:
            lines.append(
                "⏳ Действия доступа и приглашения обрабатывает клиентский бот; "
                "проверьте состояние после завершения."
            )
        if not counts.has_blockers:
            lines.append("\n✅ Блокировок больше нет. Можно вернуться к удалению.")
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
                [_button("⬅️ Панель управления", "dashboard")],
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
            [_button("⬅️ Панель управления", "dashboard")],
        ])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML", reply_markup=_markup(rows)
        )

    async def show_plan_card(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID, plan_id: UUID,
    ):
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        names = {pid: (name, status) for pid, name, status in data.plans}
        if plan_id not in names:
            raise ValueError("Тариф недоступен в выбранном проекте")
        name, status = names[plan_id]
        codes = await owner_available_currencies(factory, actor_id=actor_id)
        rows = [
            [_button(f"💰 Цена в {code}", f"price:{plan_id.hex}:{code}")]
            for code in codes
        ]
        if status == "draft":
            rows.append([_button("✅ Опубликовать тариф", "publish:" + plan_id.hex, "success")])
        rows.append([_button("⬅️ Все тарифы", "plans")])
        await bot.send_message(
            user_id,
            "📦 <b>" + escape(name) + "</b>\n"
            + "Статус: " + _status(status)
            + "\n\nВыберите валюту, чтобы задать или обновить цену.",
            parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_methods(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        lines = ["💳 <b>Способы оплаты</b>", ""]
        if not data.methods:
            lines.append("Способов оплаты пока нет.")
        else:
            lines.extend(
                "• " + escape(name) + " — " + _status(status)
                for _, name, status in data.methods
            )
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup([
                [_button("➕ Добавить способ оплаты", "newmethod", "success")],
                [_button("⬅️ Панель управления", "dashboard")],
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

    @router.message(Command("start", "menu"))
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
        try:
            if action == "new":
                await prompt(
                    bot, user_id, actor_id, step="project_name",
                    text="📁 Как назовём новый проект?\n\n"
                    "Ответьте названием — его можно будет изменить позднее.",
                )
                await query.answer()
                return
            if action == "projects":
                home = await owner_home(factory, actor_id=actor_id)
                rows = [
                    [_button("📁 " + name[:45], "select:" + pid.hex)]
                    for pid, name in home.projects[:20]
                ]
                rows.append([_button("➕ Новый проект", "new", "success")])
                await bot.send_message(user_id, "📁 Мои проекты", reply_markup=_markup(rows))
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
            project_id = await get_selected_project(factory, actor_id=actor_id)
            if project_id is None:
                raise ValueError("Сначала создайте или выберите проект")
            if action == "dashboard":
                await show_dashboard(bot, user_id, actor_id, project_id)
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
            elif action.startswith(("ob:", "obok:")):
                parts = action.split(":")
                if len(parts) != 4 or parts[1] not in ("pay", "sub", "grant", "sync"):
                    raise ValueError("Неизвестное действие")
                verb, kind, project_ref, object_ref = parts
                if decode_uuid(project_ref) != project_id:
                    raise ValueError("Открыто меню другого проекта. Обновите обязательства.")
                object_id = decode_uuid(object_ref)
                snapshot = await list_owner_obligations(
                    factory, actor_id=actor_id, project_id=project_id,
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
                                "obok:" + kind + ":" + project_ref + ":" + object_ref,
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
                name = await publish_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=UUID(hex=action.split(":", 1)[1]),
                )
                await bot.send_message(
                    user_id, f"✅ Тариф «{escape(name)}» опубликован.",
                    parse_mode="HTML",
                )
                await show_plans(bot, user_id, actor_id, project_id)
            elif action == "methods":
                await show_methods(bot, user_id, actor_id, project_id)
            elif action == "newmethod":
                await currency_picker(
                    bot, user_id, actor_id, mode="method"
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
                    factory, actor_id=actor_id, project_id=project_id
                )
                await bot.send_message(
                    user_id,
                    "📋 <b>Подготовка к запуску</b>\n\n"
                    + "\n".join(_progress(data))
                    + "\n\n🤖 " + escape(data.bot_runtime_label)
                    + "\n\nПосле завершения настроек бот запускается "
                    "автоматически. Покупателям можно давать ссылку только "
                    "после статуса «Работает».",
                    parse_mode="HTML",
                )
            elif action == "help":
                await bot.send_message(
                    user_id,
                    "❔ <b>Как запустить подписки</b>\n\n"
                    "1. Подключите своего бота через @BotFather.\n"
                    "2. Добавьте его администратором канала.\n"
                    "3. Подключите канал, создайте тариф и цену.\n"
                    "4. Добавьте способ оплаты и инструкцию.\n"
                    "5. Опубликуйте тариф и проверьте готовность.\n\n"
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
        if not _private(message):
            return
        actor_id = await actor(message.from_user)
        try:
            flow = await consume_button_flow(
                factory, actor_id=actor_id, expected_step="chat_picker",
                request_id=message.chat_shared.request_id,
            )
            if flow.project_id is None:
                raise ValueError("Проект не выбран")
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

    @router.message(SetupReplyFilter(factory))
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
        )
        if flow is None:
            return
        # Always delete BotFather credential messages before validating or storing
        # anything, even if the credential was typed incorrectly.
        unexpected_secret = bool(
            flow.step != "bot_token" and TOKEN_PATTERN.fullmatch(text)
        )
        if flow.step == "bot_token" or unexpected_secret:
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
        try:
            if flow.step == "project_name":
                await create_owner_project(factory, actor_id=actor_id, name=text)
                await bot.send_message(
                    message.chat.id,
                    "✅ Проект создан. Часовой пояс — Москва (можно изменить).",
                )
                await show_home(bot, message.chat.id, actor_id)
                return
            project_id = flow.project_id
            if project_id is None:
                raise ValueError("Проект не выбран")
            if flow.step == "bot_token":
                if not TOKEN_PATTERN.fullmatch(text):
                    raise ValueError("Неверный формат токена. Получите его в BotFather.")
                result = await register_customer_bot(
                    factory, project_id=project_id,
                    actor_telegram_user_id=message.from_user.id, token=text,
                    encryption_key=get_settings().bot_token_encryption_key,
                    central_bot_id=bot.id,
                )
                await bot.send_message(
                    message.chat.id,
                    f"✅ Бот @{escape(result.username)} подключён.\n"
                    "Теперь добавьте канал, тарифы и способы оплаты.",
                    parse_mode="HTML",
                )
                await show_bots(bot, message.chat.id, actor_id, project_id)
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

    return router
