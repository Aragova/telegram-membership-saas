"""Compact broadcast editor, saved posts and history in a connected bot."""
from __future__ import annotations

from html import escape
from uuid import UUID

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.broadcasts.service import (
    HISTORY_PAGE_SIZE,
    TEMPLATE_PAGE_SIZE,
    archive_template,
    audience_count,
    campaign_stats,
    cancel_draft,
    create_campaign,
    get_draft,
    get_template,
    list_campaigns,
    list_templates,
    record_bot_start,
    rename_template,
    save_from_message,
    set_campaign_status,
    start_draft,
)
from telegram_membership_saas.modules.broadcasts.worker import _send_post
from telegram_membership_saas.modules.owner_setup.service import (
    can_manage_project,
    cancel_owner_flow,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    decode_uuid,
    encode_uuid,
)


def _button(label: str, action: str, style: str = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data="bc:" + action, style=style)


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


class StartAudienceMiddleware(BaseMiddleware):
    """Observe private /start without intercepting existing buyer/admin handlers."""

    def __init__(self, factory, *, project_id: UUID) -> None:
        self.factory = factory
        self.project_id = project_id

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.from_user is not None:
            parts = (event.text or "").strip().split(maxsplit=1)
            raw = parts[0].split("@", 1)[0] if parts else ""
            bot = data.get("bot")
            if (
                raw == "/start" and event.chat.type == "private"
                and event.chat.id == event.from_user.id
                and bot is not None
            ):
                # The buyer flow must still work if temporary DB maintenance
                # prevents registering a marketing contact.
                try:
                    await record_bot_start(
                        self.factory, project_id=self.project_id,
                        telegram_bot_id=bot.id,
                        telegram_user_id=event.from_user.id,
                    )
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception(
                        "Could not register customer bot /start audience"
                    )
        return await handler(event, data)


class BroadcastDraftFilter(BaseFilter):
    """Only an admin with an active, bot-scoped editor captures plain messages."""

    def __init__(self, factory, *, project_id: UUID):
        self.factory = factory
        self.project_id = project_id

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if (
            message.from_user is None or message.chat.type != "private"
            or message.chat.id != message.from_user.id
            or (message.text or "").startswith("/")
        ):
            return False
        actor_id = await ensure_telegram_actor(
            self.factory, telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        if not await can_manage_project(
            self.factory, actor_id=actor_id, project_id=self.project_id,
        ):
            return False
        return await get_draft(
            self.factory, project_id=self.project_id,
            actor_id=actor_id, telegram_bot_id=bot.id,
        ) is not None


def create_broadcast_router(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
) -> Router:
    router = Router(name="customer-broadcasts")

    async def identify(user) -> UUID:
        return await ensure_telegram_actor(
            factory, telegram_id=user.id, username=user.username,
            first_name=user.first_name,
        )

    async def main(bot: Bot, uid: int, actor: UUID) -> None:
        total = await audience_count(
            factory, project_id=project_id, actor_id=actor,
            telegram_bot_id=bot.id,
        )
        await bot.send_message(
            uid, tr(
                "📨 <b>Рассылки</b>\n\n"
                "Доступных получателей: <b>" + str(total) + "</b>\n"
                "Учитываются только пользователи, нажимавшие /start у этого бота. "
                "Блокировки определяются по ошибкам доставки.",
                "📨 <b>Broadcasts</b>\n\n"
                "Reachable contacts: <b>" + str(total) + "</b>\n"
                "Only users who pressed /start in this bot are included. "
                "Blocked users are detected on delivery.",
            ),
            parse_mode="HTML",
            reply_markup=_keyboard([
                [_button(tr("✏️ Написать пост", "✏️ Write post"), "write")],
                [_button(tr("↪️ Переслать готовый пост", "↪️ Forward a post"), "forward")],
                [_button(tr("📁 Сохранённые посты", "📁 Saved posts"), "templates:0")],
                [_button(tr("📊 История рассылок", "📊 Broadcast history"), "history:0")],
                [InlineKeyboardButton(
                    text=tr("⬅️ Панель администратора", "⬅️ Admin dashboard"),
                    callback_data="own:dashboard",
                )],
            ]),
        )

    async def templates(bot: Bot, uid: int, actor: UUID, page: int) -> None:
        total, records = await list_templates(
            factory, project_id=project_id, actor_id=actor,
            telegram_bot_id=bot.id, page=page,
        )
        rows = [[_button("📝 " + x.name[:50], "post:" + encode_uuid(x.id))]
                for x in records]
        if total > (page + 1) * TEMPLATE_PAGE_SIZE or page > 0:
            nav = []
            if page:
                nav.append(_button("◀️", "templates:" + str(page - 1)))
            if total > (page + 1) * TEMPLATE_PAGE_SIZE:
                nav.append(_button("▶️", "templates:" + str(page + 1)))
            rows.append(nav)
        rows.append([_button(tr("⬅️ Рассылки", "⬅️ Broadcasts"), "home")])
        await bot.send_message(
            uid,
            tr("📁 <b>Сохранённые посты</b>\nВсего: ",
               "📁 <b>Saved posts</b>\nTotal: ") + str(total),
            parse_mode="HTML", reply_markup=_keyboard(rows),
        )

    async def post_detail(bot: Bot, uid: int, actor: UUID, post_id: UUID) -> None:
        post = await get_template(
            factory, project_id=project_id, actor_id=actor,
            telegram_bot_id=bot.id, template_id=post_id,
        )
        await bot.send_message(
            uid, tr("👁 Предпросмотр сохранённого поста: ",
                   "👁 Saved post preview: ") + escape(post.name),
            parse_mode="HTML",
        )
        try:
            await _send_post(bot, user_id=uid, content=post)
        except TelegramBadRequest:
            await bot.send_message(
                uid, tr("⚠️ Telegram больше не принимает медиа. "
                       "Замените содержимое поста.",
                       "⚠️ Telegram cannot reuse this media. Replace the post."),
            )
        tok = encode_uuid(post.id)
        await bot.send_message(
            uid, tr("📨 Что делаем с постом?", "📨 Choose an action:"),
            reply_markup=_keyboard([
                [_button(tr("🚀 Отправить всем", "🚀 Send to all"),
                         "sendask:" + tok, "success")],
                [_button(tr("✏️ Заменить содержимое", "✏️ Replace content"),
                         "edit:" + tok)],
                [_button(tr("🏷 Переименовать", "🏷 Rename"),
                         "rename:" + tok)],
                [_button(tr("🗑 Удалить из сохранённых", "🗑 Remove saved post"),
                         "deleteask:" + tok, "danger")],
                [_button(tr("⬅️ Сохранённые посты", "⬅️ Saved posts"),
                         "templates:0")],
            ]),
        )

    async def history(bot: Bot, uid: int, actor: UUID, page: int) -> None:
        total, records = await list_campaigns(
            factory, project_id=project_id, actor_id=actor,
            telegram_bot_id=bot.id, page=page,
        )
        rows = [[_button(
            ("🟢" if x.status == "running" else
             "⏸" if x.status == "paused" else "✅")
            + " " + x.created_at.strftime("%d.%m %H:%M"),
            "campaign:" + encode_uuid(x.id),
        )] for x in records]
        if page > 0 or total > (page + 1) * HISTORY_PAGE_SIZE:
            nav = []
            if page:
                nav.append(_button("◀️", "history:" + str(page - 1)))
            if total > (page + 1) * HISTORY_PAGE_SIZE:
                nav.append(_button("▶️", "history:" + str(page + 1)))
            rows.append(nav)
        rows.append([_button(tr("⬅️ Рассылки", "⬅️ Broadcasts"), "home")])
        await bot.send_message(
            uid, tr("📊 История рассылок: ", "📊 Broadcast history: ") + str(total),
            reply_markup=_keyboard(rows),
        )

    async def campaign_detail(
        bot: Bot, uid: int, actor: UUID, campaign_id: UUID,
    ) -> None:
        item, counts = await campaign_stats(
            factory, project_id=project_id, actor_id=actor,
            telegram_bot_id=bot.id, campaign_id=campaign_id,
        )
        total = sum(counts.values())
        pending = counts.get("pending", 0) + counts.get("sending", 0)
        body = (
            tr("📊 <b>Рассылка</b>", "📊 <b>Broadcast</b>")
            + "\nID: <code>" + str(item.id) + "</code>"
            + "\n" + tr("Состояние: ", "Status: ") + escape(item.status)
            + "\n" + tr("Получателей: ", "Recipients: ") + str(total)
            + "\n✅ " + tr("Отправлено: ", "Delivered: ") + str(counts.get("sent", 0))
            + "\n⏳ " + tr("Ожидают: ", "Pending: ") + str(pending)
            + "\n🚫 " + tr("Недоступны: ", "Unavailable: ") + str(counts.get("blocked", 0))
            + "\n❌ " + tr("Ошибки: ", "Failed: ") + str(counts.get("failed", 0))
            + "\n❔ " + tr("Неизвестно: ", "Unknown: ") + str(counts.get("unknown", 0))
        )
        tok = encode_uuid(item.id)
        rows = []
        if item.status == "running":
            rows.append([_button(tr("⏸ Пауза", "⏸ Pause"),
                                 "pause:" + tok)])
        elif item.status == "paused":
            rows.append([_button(tr("▶️ Продолжить", "▶️ Resume"),
                                 "resume:" + tok, "success")])
        rows.append([_button(tr("🔄 Обновить", "🔄 Refresh"),
                             "campaign:" + tok)])
        rows.append([_button(tr("⬅️ История", "⬅️ History"),
                             "history:0")])
        await bot.send_message(
            uid, body, parse_mode="HTML", reply_markup=_keyboard(rows),
        )

    @router.callback_query(F.data.startswith("bc:"))
    async def broadcast_callback(query: CallbackQuery, bot: Bot) -> None:
        if (
            query.message is None or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer("Откройте личный чат", show_alert=True)
            return
        actor = await identify(query.from_user)
        if not await can_manage_project(
            factory, actor_id=actor, project_id=project_id,
        ):
            await query.answer(tr("Нет доступа", "Access denied"), show_alert=True)
            return
        try:
            action = (query.data or "")[3:]
            pieces = action.split(":")
            command = pieces[0]
            # Only two operations accept numeric pagination.
            if command in ("templates", "history"):
                if (
                    len(pieces) != 2 or not pieces[1].isascii()
                    or not pieces[1].isdecimal() or int(pieces[1]) > 10000
                ):
                    raise ValueError("Недопустимая страница")
            if command not in ("home", "write", "forward"):
                if command not in ("templates", "history"):
                    if len(pieces) != 2:
                        raise ValueError("Неизвестное действие")
                    post_id = decode_uuid(pieces[1])

            if command in ("write", "forward"):
                # A broadcast editor must never consume a previous form's
                # payment credentials or plan-creation replies.
                await cancel_owner_flow(
                    factory, actor_id=actor, bot_scope_id=bot.id,
                )
                await start_draft(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id,
                )
                await bot.send_message(query.from_user.id, tr(
                    "✏️ Напишите пост с эмодзи и Telegram-ссылками. "
                    "Можно также отправить фото или видео с подписью. "
                    "Для выхода нажмите «Рассылки».",
                    "✏️ Send a post with emoji and Telegram links, or send "
                    "a photo/video with a caption. Use Broadcasts to exit.",
                ) if command == "write" else tr(
                    "↪️ Перешлите готовый пост сюда. Сохраним текст, "
                    "фото или видео с подписью и форматированием.",
                    "↪️ Forward a ready-made post here. We'll save its text, "
                    "photo/video, caption and formatting.",
                ))
            elif command == "home":
                await cancel_draft(factory, project_id=project_id, actor_id=actor)
                await main(bot, query.from_user.id, actor)
            elif command == "templates":
                await cancel_draft(factory, project_id=project_id, actor_id=actor)
                await templates(bot, query.from_user.id, actor, int(pieces[1]))
            elif command == "history":
                await cancel_draft(factory, project_id=project_id, actor_id=actor)
                await history(bot, query.from_user.id, actor, int(pieces[1]))
            elif command == "post":
                await post_detail(bot, query.from_user.id, actor, post_id)
            elif command == "sendask":
                post = await get_template(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id, template_id=post_id,
                )
                total = await audience_count(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id,
                )
                await bot.send_message(
                    query.from_user.id,
                    tr("Отправить «", "Send “") + escape(post.name)
                    + tr("» всем доступным пользователям? Получателей: ",
                         "” to all reachable users? Recipients: ") + str(total),
                    parse_mode="HTML", reply_markup=_keyboard([
                        [_button(tr("✅ Подтвердить рассылку", "✅ Confirm broadcast"),
                                 "sendok:" + pieces[1], "success")],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                 "post:" + pieces[1])],
                    ]),
                )
            elif command == "sendok":
                campaign, count = await create_campaign(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id, template_id=post_id,
                )
                await bot.send_message(
                    query.from_user.id,
                    tr("✅ Рассылка поставлена в очередь. Получателей: ",
                       "✅ Broadcast queued. Recipients: ") + str(count),
                )
                await campaign_detail(bot, query.from_user.id, actor, campaign.id)
            elif command in ("edit", "rename"):
                await cancel_owner_flow(
                    factory, actor_id=actor, bot_scope_id=bot.id,
                )
                await start_draft(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id,
                    mode="replace" if command == "edit" else "rename",
                    template_id=post_id,
                )
                await bot.send_message(query.from_user.id, tr(
                    "Отправьте новую версию поста (текст или медиа с подписью).",
                    "Send the replacement post (text or media with caption).",
                ) if command == "edit" else tr(
                    "Отправьте новое название для сохранённого поста.",
                    "Send a new name for the saved post.",
                ))
            elif command == "deleteask":
                await get_template(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id, template_id=post_id,
                )
                await bot.send_message(
                    query.from_user.id,
                    tr("Удалить пост из сохранённых? История отправок останется.",
                       "Remove saved post? Delivery history stays."),
                    reply_markup=_keyboard([
                        [_button(tr("🗑 Да, удалить", "🗑 Yes, remove"),
                                 "deleteok:" + pieces[1], "danger")],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                 "post:" + pieces[1])],
                    ]),
                )
            elif command == "deleteok":
                await archive_template(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id, template_id=post_id,
                )
                await templates(bot, query.from_user.id, actor, 0)
            elif command == "campaign":
                await campaign_detail(bot, query.from_user.id, actor, post_id)
            elif command in ("pause", "resume"):
                await set_campaign_status(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id, campaign_id=post_id,
                    status="paused" if command == "pause" else "running",
                )
                await campaign_detail(bot, query.from_user.id, actor, post_id)
            else:
                raise ValueError("Неизвестное действие")
            await query.answer()
        except (ValueError, PermissionError) as exc:
            await query.answer(str(exc)[:180], show_alert=True)

    @router.message(BroadcastDraftFilter(factory, project_id=project_id))
    async def receive_broadcast_post(message: Message, bot: Bot) -> None:
        actor = await identify(message.from_user)
        draft = await get_draft(
            factory, project_id=project_id,
            actor_id=actor, telegram_bot_id=bot.id,
        )
        if draft is None:
            return
        try:
            if draft.mode == "rename":
                if not message.text:
                    raise ValueError("Для переименования отправьте текст")
                post = await rename_template(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id, name=message.text,
                )
            else:
                post = await save_from_message(
                    factory, project_id=project_id, actor_id=actor,
                    telegram_bot_id=bot.id, message=message,
                )
            await bot.send_message(
                message.chat.id, tr("✅ Пост сохранён.", "✅ Post saved."),
            )
            await post_detail(bot, message.chat.id, actor, post.id)
        except (ValueError, PermissionError) as exc:
            await bot.send_message(message.chat.id, "⚠️ " + str(exc)[:350])

    return router
