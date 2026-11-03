"""Telegram owner/admin navigation for the currently configured project."""

from __future__ import annotations

from html import escape
from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
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
SCREENS = frozenset({"main", "resources", "plans", "methods", "settings", "timezone"})
STATUS_LABELS = {
    "active": "🟢 Активно",
    "disabled": "⚪ Отключено",
    "archived": "📦 Архив",
    "draft": "📝 Черновик",
}


def _button(text: str, callback_data: str, *, style: str = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)


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
            [_button("🧾 Заявки", "adm:reviews")],
            [_button("👁 Посмотреть как клиент", "adm:client")],
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
        return f"<b>{title}</b>\n\nЗаписей пока нет."
    lines = [f"<b>{title}</b>", ""]
    for entry in entries[:15]:
        state = STATUS_LABELS.get(entry.status, escape(entry.status))
        suffix = f" · {escape(entry.details)}" if entry.details else ""
        lines.append(f"• <b>{escape(entry.name)}</b>{suffix}\n  {state}")
    if len(entries) > 15:
        lines.append(f"\nПоказаны первые 15 из {len(entries)}.")
    return "\n".join(lines)


def _screen_text(screen: str, snapshot: AdminProjectSnapshot) -> str:
    if screen == "main":
        return (
            "👑 <b>ViMi Pay · Управление</b>\n\n"
            f"📁 Проект: <b>{escape(snapshot.project_name)}</b>\n"
            f"🌍 Часовой пояс: <b>{escape(snapshot.timezone)}</b>\n\n"
            f"Каналов и групп: {len(snapshot.resources)}\n"
            f"Тарифов: {len(snapshot.plans)}\n"
            f"Способов оплаты: {len(snapshot.payment_methods)}\n\n"
            "Выберите раздел управления."
        )
    if screen == "resources":
        return _render_entries("📢 Каналы / группы", snapshot.resources)
    if screen == "plans":
        return _render_entries("📦 Тарифы", snapshot.plans)
    if screen == "methods":
        return _render_entries("💳 Способы оплаты", snapshot.payment_methods)
    if screen == "settings":
        return (
            "⚙️ <b>Настройки проекта</b>\n\n"
            f"📁 {escape(snapshot.project_name)}\n"
            f"🌍 Текущий часовой пояс: <b>{escape(snapshot.timezone)}</b>\n\n"
            "Время в базе хранится в UTC. Даты подписок отображаются "
            "с учётом часового пояса проекта."
        )
    if screen == "timezone":
        return (
            "🌍 <b>Часовой пояс проекта</b>\n\n"
            f"Сейчас: <b>{escape(snapshot.timezone)}</b>\n\n"
            "Выберите зону ниже или отправьте команду "
            "<code>/timezone Region/City</code> для любой IANA-зоны.\n"
            "По умолчанию для новых проектов — Europe/Moscow."
        )
    raise ValueError("Unknown admin screen")


def _private_chat(message: Message) -> bool:
    return message.chat.type == "private" and message.from_user is not None


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

    @router.message(Command("admin"))
    async def admin_home_handler(message: Message, bot: Bot) -> None:
        if not _private_chat(message):
            return
        try:
            await show_screen(bot, message.from_user.id, "main")
        except PermissionError:
            await bot.send_message(
                chat_id=message.from_user.id, text="⛔ Нет доступа к управлению проектом."
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
                chat_id=message.from_user.id, text="⛔ Нет доступа к управлению проектом."
            )
        except ValueError as exc:
            await bot.send_message(chat_id=message.from_user.id, text=str(exc))

    @router.callback_query(F.data.startswith("adm:"))
    async def admin_navigation_handler(callback: CallbackQuery, bot: Bot) -> None:
        if callback.data is None or callback.message is None:
            return
        if callback.message.chat.type != "private":
            await callback.answer("Используйте личный чат с ботом", show_alert=True)
            return
        action = callback.data[4:]
        user_id = callback.from_user.id
        message_id = callback.message.message_id

        try:
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
                # Check manager authorization before showing a buyer's catalog.
                await load_admin_project(
                    session_factory, project_id=project_id, actor_telegram_user_id=user_id
                )
                await _send_catalog(
                    bot,
                    chat_id=user_id,
                    session_factory=session_factory,
                    project_id=project_id,
                )
            elif action == "reviews":
                await load_admin_project(
                    session_factory, project_id=project_id, actor_telegram_user_id=user_id
                )
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
                        chat_id=user_id, text="🧾 Заявок на проверку сейчас нет."
                    )
                for item in pending:
                    await bot.send_message(
                        chat_id=user_id,
                        text=_review_text(item),
                        reply_markup=_review_keyboard(item.request_id),
                    )
            else:
                await callback.answer("Неизвестное действие", show_alert=True)
                return
        except PermissionError:
            await callback.answer("Нет доступа к управлению", show_alert=True)
            return
        except ValueError as exc:
            await callback.answer(str(exc)[:180], show_alert=True)
            return

        await callback.answer()

    return router
