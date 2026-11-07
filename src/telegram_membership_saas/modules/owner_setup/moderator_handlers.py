"""Dedicated support panel in connected bots, separated from owner callbacks."""
from __future__ import annotations

import logging
from time import monotonic
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.localization import recipient_language, tr
from telegram_membership_saas.modules.owner_setup.conversations import (
    ADMIN_MESSAGE_HEADER,
    ADMIN_MESSAGE_HEADER_EN,
    BuyerConversation,
    customer_ref,
)
from telegram_membership_saas.modules.owner_setup.customer_shortcodes import (
    parse_customer_command,
)
from telegram_membership_saas.modules.owner_setup.moderators import (
    can_use_moderator_panel,
    find_moderator_customer,
    list_moderator_customers,
    list_moderator_reviews,
    project_support_recipients,
    set_moderated_customer_ban,
)
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.telegram_commerce.service import (
    decode_uuid,
    encode_uuid,
)

log = logging.getLogger(__name__)
MOD_INBOX_RU = "🛡 ViMi Pay · Покупатель "
MOD_INBOX_EN = "🛡 ViMi Pay · Customer "


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _button(text: str, data: str, style: str = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data="mod:" + data, style=style)


async def _actor_id(factory: async_sessionmaker[AsyncSession], user) -> UUID:
    return await ensure_telegram_actor(
        factory, telegram_id=user.id,
        username=user.username, first_name=user.first_name,
    )


class ModeratorOnlyFilter(BaseFilter):
    def __init__(self, factory, project_id):
        self.factory, self.project_id = factory, project_id

    async def __call__(self, message: Message) -> bool:
        if message.chat.type != "private":
            return False
        actor_id = await _actor_id(self.factory, message.from_user)
        return await can_use_moderator_panel(
            self.factory, actor_id=actor_id, project_id=self.project_id,
            support_only=True,
        )


async def render_moderator_menu_if_allowed(
    bot: Bot, user, factory: async_sessionmaker[AsyncSession], project_id: UUID,
) -> bool:
    actor_id = await _actor_id(factory, user)
    if not await can_use_moderator_panel(
        factory, actor_id=actor_id, project_id=project_id, support_only=True,
    ):
        return False
    await bot.send_message(
        user.id,
        tr("🛡 <b>Панель модератора</b>\n"
           "Покупатели, переписка и блокировки. "
           "Заявки доступны только для просмотра.\n"
           "Финансы, тарифы и права владельца недоступны.",
           "🛡 <b>Moderator panel</b>\n"
           "Customers, messaging and bans. "
           "Payment requests are read-only.\n"
           "Finance, plans and ownership are unavailable."),
        parse_mode="HTML",
        reply_markup=_keyboard([
            [_button(tr("👥 Покупатели", "👥 Customers"), "buyers:0")],
            [_button(tr("📋 Заявки (просмотр)", "📋 Requests (view only)"), "reviews")],
            [_button(tr("🔄 Обновить", "🔄 Refresh"), "home")],
        ]),
    )
    return True


def create_moderator_router(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
) -> Router:
    router = Router(name="project-support")
    # One-shot authoring state is scoped per bot process. Revocation is checked
    # again before every send; a stale cached prompt confers no permissions.
    pending: dict[int, tuple[UUID, float]] = {}

    async def show_buyer(bot: Bot, user_id: int, actor_id: UUID,
                         customer_id: UUID) -> None:
        buyer = await find_moderator_customer(
            factory, actor_id=actor_id, project_id=project_id,
            customer_id=customer_id,
        )
        cid = encode_uuid(buyer.id)
        status = (tr("🚫 Заблокирован", "🚫 Banned") if buyer.is_banned
                  else tr("✅ Не заблокирован", "✅ Not banned"))
        await bot.send_message(
            user_id,
            tr("👤 <b>Покупатель</b>", "👤 <b>Customer</b>") + "\n"
            + ("@" + buyer.username if buyer.username else str(buyer.telegram_id))
            + "\nTelegram ID: " + str(buyer.telegram_id) + "\n"
            + tr("Счетов: ", "Invoices: ") + str(buyer.invoice_count)
            + "\n" + status,
            parse_mode="HTML",
            reply_markup=_keyboard([
                [_button(tr("✉️ Написать", "✉️ Message"), "compose:" + cid)],
                [_button(tr("✅ Разблокировать", "✅ Unban") if buyer.is_banned
                         else tr("🚫 Заблокировать", "🚫 Ban"),
                         "banask:" + cid + (":0" if buyer.is_banned else ":1"),
                         "success" if buyer.is_banned else "danger")],
                [_button(tr("⬅️ Покупатели", "⬅️ Customers"), "buyers:0")],
            ]),
        )

    async def send_to_buyer(bot: Bot, buyer_id: int, body: str) -> None:
        async with recipient_language(factory, buyer_id):
            await bot.send_message(
                buyer_id,
                tr(ADMIN_MESSAGE_HEADER, ADMIN_MESSAGE_HEADER_EN)
                + "\n\n" + body + "\n\n"
                + tr("↩️ Ответьте на это сообщение или нажмите кнопку.",
                     "↩️ Reply to this message or tap the button."),
                reply_markup=_keyboard([[
                    InlineKeyboardButton(
                        text=tr("↩️ Ответить", "↩️ Reply"), callback_data="chat:reply",
                    ),
                ]]),
            )

    @router.message(Command("moderator", "mod"))
    async def moderator_command(message: Message, bot: Bot) -> None:
        if message.chat.type != "private":
            return
        allowed = await render_moderator_menu_if_allowed(
            bot, message.from_user, factory, project_id,
        )
        if not allowed:
            await bot.send_message(
                message.chat.id,
                tr("🛡 Доступ модератора не назначен.\n"
                   "Ваш Telegram ID: ", "🛡 Moderator access not assigned.\n"
                   "Your Telegram ID: ") + str(message.from_user.id)
                + tr("\nПередайте ID владельцу этого бота.",
                     "\nGive this ID to the bot's owner."),
            )

    @router.message(
        Command("start", "menu"), ModeratorOnlyFilter(factory, project_id),
    )
    async def moderator_menu_command(message: Message, bot: Bot) -> None:
        pending.pop(message.from_user.id, None)
        await render_moderator_menu_if_allowed(
            bot, message.from_user, factory, project_id,
        )

    @router.message(
        F.text.regexp(r"^/(?:u[0-9a-z]{1,13}|c[a-z2-7]{26})$"),
        ModeratorOnlyFilter(factory, project_id),
    )
    async def moderator_customer_shortcut(message: Message, bot: Bot) -> None:
        actor_id = await _actor_id(factory, message.from_user)
        try:
            tid, customer = parse_customer_command(message.text)
            buyer = await find_moderator_customer(
                factory, actor_id=actor_id, project_id=project_id,
                customer_id=customer, telegram_id=tid,
            )
            await show_buyer(bot, message.chat.id, actor_id, buyer.id)
        except (ValueError, PermissionError):
            await bot.send_message(message.chat.id, tr(
                "Покупатель не найден в этом боте.",
                "Customer not found in this bot.",
            ))

    @router.callback_query(F.data.startswith("mod:"))
    async def moderator_callback(query: CallbackQuery, bot: Bot) -> None:
        if (
            query.message is None or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer()
            return
        actor_id = await _actor_id(factory, query.from_user)
        if not await can_use_moderator_panel(
            factory, actor_id=actor_id, project_id=project_id,
            support_only=True,
        ):
            pending.pop(query.from_user.id, None)
            await query.answer(tr("Доступ отозван", "Access revoked"), show_alert=True)
            return
        action = query.data[4:]
        try:
            if action == "home":
                await render_moderator_menu_if_allowed(
                    bot, query.from_user, factory, project_id,
                )
            elif action.startswith("buyers:"):
                value = action.split(":", 1)[1]
                if not value.isascii() or not value.isdecimal():
                    raise ValueError("Неверная страница")
                page = int(value)
                total, users = await list_moderator_customers(
                    factory, actor_id=actor_id, project_id=project_id, page=page,
                )
                buttons = [[_button(
                    ("🚫 " if buyer.is_banned else "👤 ")
                    + ("@" + buyer.username if buyer.username else
                       str(buyer.telegram_id)),
                    "buyer:" + encode_uuid(buyer.id),
                )] for buyer in users]
                nav = []
                if page:
                    nav.append(_button("◀️", "buyers:" + str(page - 1)))
                if (page + 1) * 8 < total:
                    nav.append(_button("▶️", "buyers:" + str(page + 1)))
                if nav:
                    buttons.append(nav)
                buttons.append([_button(tr("⬅️ Меню", "⬅️ Menu"), "home")])
                await bot.send_message(
                    query.from_user.id,
                    tr("👥 Покупателей: ", "👥 Customers: ") + str(total),
                    reply_markup=_keyboard(buttons),
                )
            elif action.startswith("buyer:"):
                await show_buyer(
                    bot, query.from_user.id, actor_id,
                    decode_uuid(action.split(":", 1)[1]),
                )
            elif action == "reviews":
                items = await list_moderator_reviews(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                rows = [[_button(
                    "👤 " + ("@" + item.username if item.username else
                            str(item.telegram_id))
                    + " · " + item.plan_name[:24],
                    "find:" + str(item.telegram_id),
                )] for item in items]
                rows.append([_button(tr("⬅️ Меню", "⬅️ Menu"), "home")])
                await bot.send_message(
                    query.from_user.id,
                    tr("📋 <b>Заявки на проверке (только просмотр)</b>\n"
                       "Суммы, чеки и кнопки подтверждения недоступны.\n"
                       "Заявок: ",
                       "📋 <b>Pending reviews (read only)</b>\n"
                       "Amounts, receipts and approval are unavailable.\n"
                       "Requests: ") + str(len(items)),
                    parse_mode="HTML", reply_markup=_keyboard(rows),
                )
            elif action.startswith("find:"):
                tid = int(action.split(":", 1)[1])
                buyer = await find_moderator_customer(
                    factory, actor_id=actor_id, project_id=project_id,
                    telegram_id=tid,
                )
                await show_buyer(bot, query.from_user.id, actor_id, buyer.id)
            elif action.startswith("compose:"):
                cid = decode_uuid(action.split(":", 1)[1])
                await find_moderator_customer(
                    factory, actor_id=actor_id, project_id=project_id,
                    customer_id=cid,
                )
                pending[query.from_user.id] = (cid, monotonic() + 180)
                await bot.send_message(
                    query.from_user.id,
                    tr("✉️ Напишите текст следующим сообщением "
                       "(до 3500 символов). /mod — отмена.",
                       "✉️ Send your next text message "
                       "(up to 3500 characters). /mod to cancel."),
                    reply_markup=_keyboard([[
                        _button(tr("✖️ Отмена", "✖️ Cancel"), "cancel"),
                    ]]),
                )
            elif action == "cancel":
                pending.pop(query.from_user.id, None)
                await render_moderator_menu_if_allowed(
                    bot, query.from_user, factory, project_id,
                )
            elif action.startswith(("banask:", "banok:")):
                verb, cid, flag = action.split(":", 2)
                if flag not in ("0", "1"):
                    raise ValueError("Неверное действие")
                customer = decode_uuid(cid)
                buyer = await find_moderator_customer(
                    factory, actor_id=actor_id, project_id=project_id,
                    customer_id=customer,
                )
                banned = flag == "1"
                if verb == "banask":
                    await bot.send_message(
                        query.from_user.id,
                        tr("Подтвердите блокировку. "
                           "Новые счета и чеки будут недоступны, "
                           "но оплаченные права сохранятся.",
                           "Confirm ban. New invoices and receipts "
                           "will be blocked, existing paid access stays.")
                        if banned else tr(
                            "Разблокировать покупателя?",
                            "Unban customer?",
                        ),
                        reply_markup=_keyboard([
                            [_button(tr("✅ Подтвердить", "✅ Confirm"),
                                     "banok:" + cid + ":" + flag,
                                     "danger" if banned else "success")],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "buyer:" + cid)],
                        ]),
                    )
                else:
                    await set_moderated_customer_ban(
                        factory, actor_id=actor_id, project_id=project_id,
                        customer_id=buyer.id, banned=banned,
                    )
                    await show_buyer(bot, query.from_user.id, actor_id, buyer.id)
            else:
                raise ValueError("Неизвестное действие")
            await query.answer()
        except (ValueError, PermissionError) as exc:
            await query.answer(str(exc)[:190], show_alert=True)

    class ModeratorComposeFilter(BaseFilter):
        async def __call__(self, message: Message) -> bool:
            if (
                message.chat.type != "private" or not message.text
                or message.text.startswith("/") or message.reply_to_message
            ):
                return False
            entry = pending.get(message.from_user.id)
            return bool(entry and entry[1] > monotonic())

    @router.message(ModeratorComposeFilter())
    async def moderator_compose(message: Message, bot: Bot) -> None:
        entry = pending.pop(message.from_user.id, None)
        if entry is None or entry[1] <= monotonic():
            return
        actor_id = await _actor_id(factory, message.from_user)
        if not await can_use_moderator_panel(
            factory, actor_id=actor_id, project_id=project_id,
            support_only=True,
        ):
            return
        body = message.text.strip()
        if not 1 <= len(body) <= 3500:
            await bot.send_message(message.chat.id, tr(
                "Нужен текст от 1 до 3500 символов.",
                "Text must be 1–3500 characters.",
            ))
            return
        try:
            buyer = await find_moderator_customer(
                factory, actor_id=actor_id, project_id=project_id,
                customer_id=entry[0],
            )
            await send_to_buyer(bot, buyer.telegram_id, body)
        except AiogramError:
            await bot.send_message(message.chat.id, tr(
                "⚠️ Сообщение не доставлено.", "⚠️ Message not delivered.",
            ))
            return
        except (PermissionError, ValueError):
            await bot.send_message(message.chat.id, tr(
                "⚠️ Нет доступа к этому покупателю.", "⚠️ Customer is unavailable.",
            ))
            return
        await bot.send_message(message.chat.id, tr(
            "✅ Сообщение отправлено.", "✅ Message sent.",
        ))

    return router


async def notify_support_of_buyer_reply(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, project_id: UUID, info: BuyerConversation, body: str,
) -> None:
    """Best-effort support inbox. Original owner delivery remains authoritative."""
    recipients = await project_support_recipients(
        factory, project_id=project_id,
    )
    for target in recipients:
        try:
            async with recipient_language(factory, target):
                await bot.send_message(
                    target,
                    tr(MOD_INBOX_RU, MOD_INBOX_EN) + customer_ref(info)
                    + "\n👤 " + ("@" + info.username if info.username else
                                 str(info.customer_telegram_id))
                    + "\n\n" + body,
                    reply_markup=_keyboard([
                        [_button(tr("✉️ Ответить", "✉️ Reply"),
                                 "compose:" + encode_uuid(info.customer_id)),
                         _button(tr("👤 Карточка", "👤 Customer"),
                                 "buyer:" + encode_uuid(info.customer_id))],
                    ]),
                )
        except AiogramError:
            log.warning("Could not send support notification to %s", target)
