"""Connected-bot ownership transfer UI: two accounts, two approvals, no tokens in callbacks."""
from __future__ import annotations

import logging
from time import monotonic
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.bots.service import TOKEN_PATTERN
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.owner_setup.transfers import (
    accept_transfer_invite,
    cancel_transfer_invite,
    create_transfer_invite,
    finalize_transfer,
    get_transfer_invite,
    list_transfer_invites,
)
from telegram_membership_saas.modules.projects import Project, ProjectStatus
from telegram_membership_saas.modules.telegram_commerce.service import (
    decode_uuid,
    encode_uuid,
)
from telegram_membership_saas.tenant_context import TenantContext

log = logging.getLogger(__name__)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _button(label: str, data: str, *, danger: bool = False) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=label, callback_data="xfr:" + data,
        style="danger" if danger else "primary",
    )


def create_transfer_router(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
) -> Router:
    router = Router(name="secure-owner-transfer")
    recipient_entry: dict[int, float] = {}
    token_entry: dict[int, tuple[UUID, float]] = {}

    async def _actor(user) -> UUID:
        return await ensure_telegram_actor(
            factory, telegram_id=user.id,
            username=user.username, first_name=user.first_name,
        )

    async def _is_owner(actor_id: UUID) -> bool:
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id),
        ) as session:
            return await session.scalar(
                select(Project.id).where(
                    Project.id == project_id,
                    Project.owner_user_id == actor_id,
                    Project.status == ProjectStatus.ACTIVE.value,
                )
            ) is not None

    async def _show_home(bot: Bot, user_id: int, actor_id: UUID) -> None:
        is_owner = await _is_owner(actor_id)
        all_items = await list_transfer_invites(factory, actor_id=actor_id)
        relevant = [t for t in all_items if t.project_id == project_id]
        if not is_owner and not any(t.to_user_id == actor_id for t in relevant):
            await bot.send_message(user_id, tr(
                "🔁 Приглашений на передачу бота нет.",
                "🔁 No bot transfer invitations.",
            ))
            return
        rows = []
        for item in relevant:
            owner = item.from_user_id == actor_id
            state = (
                tr("Получатель подтвердил", "Recipient accepted")
                if item.status == "accepted"
                else tr("Ожидает получателя", "Awaiting recipient")
            )
            rows.append([_button(
                ("📤 " if owner else "📥 ") + "@"
                + item.bot_username + " · " + state,
                "view:" + encode_uuid(item.id),
            )])
        if is_owner:
            rows.append([_button(
                tr("➕ Передать этого бота", "➕ Transfer this bot"), "start",
            )])
        await bot.send_message(
            user_id,
            tr(
                "🔁 <b>Передача управления ботом</b>\n\n"
                "Здесь передаётся только этот бот и его проект. "
                "Подключения других каналов не меняются.\n"
                "Получатель должен предварительно получить бота через "
                "@BotFather и быть администратором его каналов.\n"
                "Платежи, подписки и история сохраняются. Новому владельцу "
                "придётся заново включить собственные способы оплаты.",
                "🔁 <b>Bot management transfer</b>\n\n"
                "Only this bot and its project move; other connections stay.\n"
                "The recipient must first receive the bot in @BotFather and "
                "be an admin in its channels.\n"
                "Payments, subscriptions and history stay intact. "
                "The new owner must configure their own payment methods.",
            ),
            parse_mode="HTML", reply_markup=_markup(rows) if rows else None,
        )

    async def _show_invite(bot: Bot, user_id: int, actor_id: UUID,
                           transfer_id: UUID) -> None:
        item = await get_transfer_invite(
            factory, actor_id=actor_id, transfer_id=transfer_id,
        )
        if item.project_id != project_id:
            raise PermissionError("Эта передача относится к другому боту")
        owner = item.from_user_id == actor_id
        buttons: list[list[InlineKeyboardButton]] = []
        token = encode_uuid(item.id)
        if not owner and item.status == "pending":
            buttons.append([_button(
                tr("✅ Подтвердить и ввести токен", "✅ Confirm and enter token"),
                "accept:" + token,
            )])
        if owner and item.status == "accepted":
            buttons.append([_button(
                tr("✅ Завершить передачу", "✅ Finalize transfer"),
                "finalask:" + token,
            )])
        if item.status in ("pending", "accepted"):
            buttons.append([_button(
                tr("✖️ Отменить передачу", "✖️ Cancel transfer"),
                "cancelask:" + token, danger=True,
            )])
        buttons.append([_button(tr("⬅️ Назад", "⬅️ Back"), "home")])
        await bot.send_message(
            user_id,
            "🤖 @" + item.bot_username + "\n"
            + tr("Передача: ", "Transfer: ") + item.status + "\n"
            + tr("Срок: ", "Expires: ") + item.expires_at.strftime("%d.%m %H:%M UTC")
            + "\n\n" + tr(
                "Новый владелец получит управление проектом, историей продаж "
                "и активными подписками после отдельного подтверждения старого "
                "владельца. Перевод @BotFather выполняется в самом Telegram.",
                "New owner will manage the project, sale history, and active "
                "subscriptions only after the current owner confirms. "
                "BotFather ownership changes separately in Telegram.",
            ),
            reply_markup=_markup(buttons),
        )

    @router.message(Command("transfer"))
    async def transfer_command(message: Message, bot: Bot) -> None:
        if message.chat.type != "private":
            return
        recipient_entry.pop(message.from_user.id, None)
        token_entry.pop(message.from_user.id, None)
        await _show_home(bot, message.chat.id, await _actor(message.from_user))

    @router.callback_query(F.data == "own:transfer")
    @router.callback_query(F.data.startswith("xfr:"))
    async def transfer_callback(query: CallbackQuery, bot: Bot) -> None:
        if (
            query.message is None or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer()
            return
        actor_id = await _actor(query.from_user)
        action = "home" if query.data == "own:transfer" else query.data[4:]
        try:
            if action == "home":
                await _show_home(bot, query.from_user.id, actor_id)
            elif action == "start":
                if not await _is_owner(actor_id):
                    raise PermissionError("Передачу начинает только владелец")
                recipient_entry[query.from_user.id] = monotonic() + 180
                await bot.send_message(
                    query.from_user.id,
                    tr(
                        "📤 <b>Кому передать управление?</b>\n"
                        "Введите числовой Telegram ID получателя следующим "
                        "сообщением. Он должен заранее открыть этого бота через "
                        "/start. Для отмены — /transfer.",
                        "📤 <b>New owner</b>\n"
                        "Send the recipient's numeric Telegram ID as your "
                        "next message. They must first open this bot via "
                        "/start. Use /transfer to cancel.",
                    ),
                    parse_mode="HTML",
                )
            elif action.startswith("view:"):
                await _show_invite(
                    bot, query.from_user.id, actor_id,
                    decode_uuid(action.split(":", 1)[1]),
                )
            elif action.startswith("accept:"):
                tid = decode_uuid(action.split(":", 1)[1])
                item = await get_transfer_invite(factory, actor_id=actor_id,
                                                  transfer_id=tid)
                if item.project_id != project_id or item.to_user_id != actor_id:
                    raise PermissionError("Это приглашение не для вас")
                if item.status != "pending":
                    raise ValueError("Это приглашение уже обработано")
                token_entry[query.from_user.id] = (tid, monotonic() + 180)
                await bot.send_message(
                    query.from_user.id,
                    tr(
                        "🔐 <b>Подтверждение нового владельца</b>\n\n"
                        "1. Сначала получите бот через @BotFather.\n"
                        "2. Убедитесь, что вы администратор его канала.\n"
                        "3. Вставьте НОВЫЙ токен именно этого бота. Сообщение "
                        "с токеном будет немедленно удалено.\n"
                        "Время ввода — 3 минуты. /transfer — отмена.",
                        "🔐 <b>Verify new ownership</b>\n\n"
                        "1. Receive the bot through @BotFather.\n"
                        "2. Be an administrator of its channel.\n"
                        "3. Send its NEW token; the bot will immediately "
                        "delete that message.\n"
                        "You have 3 minutes. /transfer cancels.",
                    ),
                    parse_mode="HTML",
                )
            elif action.startswith(("cancelask:", "cancelok:")):
                verb, value = action.split(":", 1)
                tid = decode_uuid(value)
                item = await get_transfer_invite(factory, actor_id=actor_id, transfer_id=tid)
                if item.project_id != project_id:
                    raise PermissionError("Чужой бот")
                if verb == "cancelask":
                    await bot.send_message(
                        query.from_user.id,
                        tr("Отменить передачу?", "Cancel the transfer?"),
                        reply_markup=_markup([
                            [_button(tr("✅ Да, отменить", "✅ Confirm cancel"),
                                     "cancelok:" + value, danger=True)],
                            [_button(tr("⬅️ Назад", "⬅️ Back"), "view:" + value)],
                        ]),
                    )
                else:
                    await cancel_transfer_invite(
                        factory, actor_id=actor_id, transfer_id=tid,
                    )
                    await _show_home(bot, query.from_user.id, actor_id)
            elif action.startswith(("finalask:", "finalok:")):
                verb, value = action.split(":", 1)
                tid = decode_uuid(value)
                item = await get_transfer_invite(factory, actor_id=actor_id, transfer_id=tid)
                if item.project_id != project_id or item.from_user_id != actor_id:
                    raise PermissionError("Только текущий владелец")
                if verb == "finalask":
                    await bot.send_message(
                        query.from_user.id,
                        tr(
                            "⚠️ <b>Окончательно передать управление?</b>\n"
                            "После этого вы потеряете доступ к настройкам "
                            "этого проекта. Незавершённые счета блокируют "
                            "передачу. Все старые способы оплаты выключатся; "
                            "оплаченные подписки и история останутся. "
                            "Действие нельзя отменить одной кнопкой.",
                            "⚠️ <b>Finalize ownership transfer?</b>\n"
                            "You lose admin access to this project. "
                            "Unfinished invoices block the transfer. "
                            "Previous payment methods are disabled; "
                            "existing paid subscriptions and history stay. "
                            "This cannot be undone with a button.",
                        ),
                        parse_mode="HTML", reply_markup=_markup([
                            [_button(tr("✅ Передать навсегда", "✅ Transfer ownership"),
                                     "finalok:" + value)],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "view:" + value)],
                        ]),
                    )
                else:
                    await finalize_transfer(
                        factory, actor_id=actor_id, project_id=project_id,
                        transfer_id=tid,
                    )
                    await bot.send_message(
                        query.from_user.id, tr(
                            "✅ Управление передано. Старые способы оплаты "
                            "выключены. Бот автоматически перезапустится "
                            "с новым токеном.",
                            "✅ Management transferred. Old payment methods "
                            "are disabled. Bot restarts with the new token.",
                        ),
                    )
            else:
                raise ValueError("Неизвестное действие")
            await query.answer()
        except (PermissionError, ValueError) as exc:
            await query.answer(str(exc)[:185], show_alert=True)

    class TransferInputFilter(BaseFilter):
        async def __call__(self, message: Message) -> bool:
            if message.chat.type != "private" or not message.text:
                return False
            if message.text.startswith("/"):
                return False
            now = monotonic()
            return (
                recipient_entry.get(message.from_user.id, 0) > now
                or (
                    message.from_user.id in token_entry
                    and token_entry[message.from_user.id][1] > now
                )
            )

    @router.message(TransferInputFilter())
    async def transfer_input(message: Message, bot: Bot) -> None:
        uid = message.from_user.id
        entry = token_entry.pop(uid, None)
        actor_id = await _actor(message.from_user)
        if entry is not None:
            tid, deadline = entry
            # Token must never be retained in Telegram, even if invalid.
            try:
                deleted = await bot.delete_message(message.chat.id, message.message_id)
            except AiogramError:
                deleted = False
            if not deleted:
                await bot.send_message(message.chat.id, tr(
                    "⚠️ Токен не удалось удалить. Смените его в @BotFather.",
                    "⚠️ Could not delete token; rotate it in @BotFather.",
                ))
                return
            if deadline < monotonic() or not TOKEN_PATTERN.fullmatch(message.text):
                await bot.send_message(message.chat.id, tr(
                    "⚠️ Истёк срок или неверный формат токена. Откройте /transfer.",
                    "⚠️ Token expired or malformed. Open /transfer.",
                ))
                return
            try:
                await accept_transfer_invite(
                    factory, actor_id=actor_id,
                    actor_telegram_id=uid, transfer_id=tid,
                    fresh_token=message.text,
                    encryption_key=get_settings().bot_token_encryption_key,
                )
            except (ValueError, PermissionError) as exc:
                await bot.send_message(message.chat.id, "⚠️ " + str(exc))
                return
            await bot.send_message(message.chat.id, tr(
                "✅ Токен и права на канал проверены. Ожидаем окончательного "
                "подтверждения прежнего владельца.",
                "✅ Bot token and channel permissions verified. "
                "Waiting for final approval by the previous owner.",
            ))
            return

        if recipient_entry.pop(uid, 0) <= monotonic():
            return
        tid = message.text.strip()
        if not tid.isascii() or not tid.isdecimal() or len(tid) > 19:
            await bot.send_message(message.chat.id, tr(
                "Нужен числовой Telegram ID. Повторите через /transfer.",
                "A numeric Telegram ID is required. Restart with /transfer.",
            ))
            return
        try:
            invite = await create_transfer_invite(
                factory, actor_id=actor_id, project_id=project_id,
                recipient_telegram_id=int(tid),
            )
        except (ValueError, PermissionError) as exc:
            await bot.send_message(message.chat.id, "⚠️ " + str(exc))
            return
        await bot.send_message(message.chat.id, tr(
            "✅ Приглашение создано. Получатель открывает этого бота "
            "и отправляет /transfer. Срок — 48 часов.",
            "✅ Invitation created. Recipient opens this bot "
            "and sends /transfer. Valid for 48 hours.",
        ))
        try:
            await bot.send_message(
                int(tid),
                tr(
                    "📥 Вам предложили управление ботом @",
                    "📥 You have been invited to manage @",
                ) + invite.bot_username
                + tr("\nОткройте /transfer, чтобы подтвердить передачу.",
                     "\nOpen /transfer to confirm the transfer."),
            )
        except AiogramError:
            await bot.send_message(message.chat.id, tr(
                "Получатель пока не принимает сообщения бота. "
                "Попросите его самостоятельно открыть /transfer.",
                "Recipient cannot receive messages yet. "
                "Ask them to open /transfer themselves.",
            ))

    return router
