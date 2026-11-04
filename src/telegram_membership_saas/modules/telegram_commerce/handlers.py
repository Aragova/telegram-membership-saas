from __future__ import annotations

from datetime import datetime
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    catalog_screen,
    list_buyer_offers,
    offer_screen,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    CheckoutOption,
    ReviewItem,
    ReviewResult,
    cancel_checkout_for_telegram_user,
    create_checkout_for_telegram_user,
    decode_uuid,
    encode_uuid,
    format_amount,
    format_period,
    list_pending_reviews_for_actor,
    require_awaiting_receipt_for_telegram_user,
    review_payment_request_from_telegram,
    submit_checkout_for_telegram_user,
)

RECEIPT_REPLY_PREFIX = "Подтверждение оплаты по заявке: "
ALLOWED_RECEIPT_MIME_TYPES = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "image/webp"}
)


def _receipt_request_id(message: Message, *, bot_id: int) -> UUID | None:
    """Derive the request ID from the bot's original ForceReply prompt, never user text."""
    if message.chat.type != "private":
        return None
    prompt = message.reply_to_message
    if prompt is None or prompt.from_user is None or prompt.from_user.id != bot_id:
        return None
    header = (prompt.text or "").split("\n", 1)[0]
    if not header.startswith(RECEIPT_REPLY_PREFIX):
        return None
    try:
        return UUID(header[len(RECEIPT_REPLY_PREFIX) :])
    except ValueError:
        return None


class ReceiptReplyFilter(BaseFilter):
    """Handle only replies to our receipt prompt, leaving commands and other replies alone."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if message.text and message.text.startswith("/"):
            return False
        return _receipt_request_id(message, bot_id=bot.id) is not None


def _acceptable_receipt(message: Message) -> bool:
    if message.photo:
        return True
    return bool(
        message.document is not None
        and message.document.mime_type in ALLOWED_RECEIPT_MIME_TYPES
    )


def _option_label(option: CheckoutOption) -> str:
    return (
        f"{option.plan_name} · {format_period(option.duration_value, option.duration_unit)} · "
        f"{format_amount(option.amount, option.currency_precision)} {option.currency_code} · "
        f"{option.payment_method_name}"
    )


def _checkout_keyboard(request_id: UUID) -> InlineKeyboardMarkup:
    token = encode_uuid(request_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил",
                    style="success",
                    callback_data=f"paid:{token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отменить",
                    style="danger",
                    callback_data=f"cancel:{token}",
                )
            ],
        ]
    )


def _review_keyboard(request_id: UUID) -> InlineKeyboardMarkup:
    token = encode_uuid(request_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить",
                    style="success",
                    callback_data=f"approve:{token}",
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    style="danger",
                    callback_data=f"reject:{token}",
                ),
            ]
        ]
    )


def _review_text(item: ReviewItem) -> str:
    username = f"@{item.customer_username}" if item.customer_username else "без username"
    amount = format_amount(item.amount, 18)
    return (
        "🧾 Заявка на подтверждение\n\n"
        f"👤 Покупатель: {username}\n"
        f"📦 Тариф: {item.plan_name}\n"
        f"💳 Оплата: {amount} {item.currency_code}\n"
        f"Способ: {item.payment_method_name}\n"
        f"🔖 Заявка: {item.request_id}\n\n"
        "Проверьте поступление средств в банке, затем примите решение."
    )


def _subscription_end_text(ends_at: datetime | None, timezone: str) -> str:
    if ends_at is None:
        return "Бессрочно"
    return f"{ends_at.astimezone(ZoneInfo(timezone)):%d.%m.%Y %H:%M} ({timezone})"


def _admin_review_result_text(result: ReviewResult, *, approve: bool) -> str:
    item = result.item
    expiration = escape(
        _subscription_end_text(result.subscription_period_end, result.project_timezone)
    )
    username = f"@{item.customer_username}" if item.customer_username else str(
        item.customer_telegram_user_id
    )
    headline = "✅ <b>Оплата подтверждена</b>" if approve else "❌ <b>Оплата отклонена</b>"
    details = (
        f"👤 Покупатель: {escape(username)}\n"
        f"📦 Тариф: {escape(item.plan_name)}\n"
        f"💳 Сумма: {format_amount(item.amount, 18)} {escape(item.currency_code)}\n"
    )
    if approve:
        details += (
            "🟢 Статус подписки: активна\n"
            f"📅 Действует до: "
            f"{expiration}\n\n"
            "Бот автоматически предоставит доступ покупателю."
        )
    else:
        details += "\nЗаявка закрыта. Подписка не активирована."
    return f"{headline}\n\n{details}"


def _customer_review_result_text(result: ReviewResult, *, approve: bool) -> str:
    expiration = escape(
        _subscription_end_text(result.subscription_period_end, result.project_timezone)
    )
    if approve:
        return (
            "✅ <b>Подписка активирована</b>\n\n"
            f"📦 {escape(result.item.plan_name)}\n"
            f"📅 До: "
            f"{expiration}\n\n"
            "🔐 Если вы ещё не в канале, бот пришлёт кнопку для вступления."
        )
    return (
        "❌ <b>Оплата отклонена</b>\n\n"
        f"Тариф: {escape(result.item.plan_name)}\n"
        "Если произошла ошибка, свяжитесь с администратором канала."
    )


async def _safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except AiogramError:
        return


async def _send_catalog(
    bot: Bot,
    *,
    chat_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    project_id: UUID,
    currency: str | None = None,
    page: int = 0,
    message_id: int | None = None,
    price_id: UUID | None = None,
) -> bool:
    """Build a fresh, project-isolated catalog and edit the current screen."""
    async with system_project_session_scope(session_factory, project_id) as session:
        offers = await list_buyer_offers(session, project_id=project_id)
    if price_id is not None:
        rendered = offer_screen(offers, price_id=price_id, page=page)
        if rendered is None:
            return False
        text, keyboard = rendered
    else:
        text, keyboard = catalog_screen(
            offers, currency=currency, page=page,
        )
    if message_id is not None:
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id,
                parse_mode="HTML", reply_markup=keyboard,
            )
            return True
        except AiogramError as exc:
            # Repeated tap of the selected currency changes no message.
            if "message is not modified" in str(exc).lower():
                return True
            # Deleted/old Telegram messages may no longer be editable.
            pass
    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML",
        reply_markup=keyboard,
    )
    return True


def _buyer_screen_callback(callback: CallbackQuery) -> bool:
    return bool(
        callback.message is not None
        and callback.message.chat.type == "private"
        and callback.message.chat.id == callback.from_user.id
    )


def create_telegram_commerce_router(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
) -> Router:
    router = Router(name="telegram-commerce")

    @router.message(Command("start", "plans"))
    async def catalog_handler(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        await _send_catalog(
            bot,
            chat_id=message.from_user.id,
            session_factory=session_factory,
            project_id=project_id,
        )

    @router.callback_query(F.data.startswith("cat:"))
    async def currency_handler(callback: CallbackQuery, bot: Bot) -> None:
        if not _buyer_screen_callback(callback):
            await callback.answer("Откройте личный чат с ботом", show_alert=True)
            return
        try:
            _, currency, page_token = (callback.data or "").split(":", 2)
            if not currency.isascii() or not currency.isalnum():
                raise ValueError("Некорректная валюта")
            if not page_token.isascii() or not page_token.isdecimal():
                raise ValueError("Некорректная страница")
            page = int(page_token)
            if page > 10000:
                raise ValueError("Некорректная страница")
            await _send_catalog(
                bot, chat_id=callback.from_user.id,
                session_factory=session_factory, project_id=project_id,
                currency=currency, page=page,
                message_id=callback.message.message_id,
            )
        except ValueError:
            await callback.answer(
                "Список изменился. Откройте /plans заново.",
                show_alert=True,
            )
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("offer:"))
    async def offer_handler(callback: CallbackQuery, bot: Bot) -> None:
        if not _buyer_screen_callback(callback):
            await callback.answer("Откройте личный чат с ботом", show_alert=True)
            return
        try:
            _, price_token, page_token = (callback.data or "").split(":", 2)
            if not page_token.isascii() or not page_token.isdecimal():
                raise ValueError("Некорректная страница")
            page = int(page_token)
            if page > 10000:
                raise ValueError("Некорректная страница")
            matched = await _send_catalog(
                bot, chat_id=callback.from_user.id,
                session_factory=session_factory, project_id=project_id,
                price_id=decode_uuid(price_token), page=page,
                message_id=callback.message.message_id,
            )
            if not matched:
                await callback.answer(
                    "Тариф больше недоступен. Откройте /plans заново.",
                    show_alert=True,
                )
                return
        except ValueError:
            await callback.answer(
                "Тариф больше недоступен. Откройте /plans заново.",
                show_alert=True,
            )
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("buy:"))
    async def buy_handler(callback: CallbackQuery, bot: Bot) -> None:
        if callback.data is None:
            return
        try:
            _, price_token, method_token = callback.data.split(":", 2)
            plan_price_id = decode_uuid(price_token)
            payment_method_id = decode_uuid(method_token)
            async with system_project_session_scope(session_factory, project_id) as session:
                checkout = await create_checkout_for_telegram_user(
                    session,
                    project_id=project_id,
                    telegram_user_id=callback.from_user.id,
                    username=callback.from_user.username,
                    first_name=callback.from_user.first_name,
                    plan_price_id=plan_price_id,
                    payment_method_id=payment_method_id,
                )
        except (ValueError, PermissionError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        await callback.answer()
        amount = format_amount(
            checkout.request.final_amount,
            checkout.option.currency_precision,
        )
        period = escape(
            format_period(checkout.option.duration_value, checkout.option.duration_unit)
        )
        await bot.send_message(
            chat_id=callback.from_user.id,
            text=(
                "💎 <b>Оформление подписки</b>\n\n"
                f"📦 <b>{escape(checkout.option.plan_name)}</b>\n"
                f"⏳ {period}\n"
                f"💳 <b>{amount} {escape(checkout.request.currency_code)}</b>\n"
                f"Способ: {escape(checkout.option.payment_method_name)}\n\n"
                f"{escape(checkout.instruction)}\n\n"
                "После оплаты нажмите <b>«Я оплатил»</b> и прикрепите чек. "
                "Поступление проверит администратор."
            ),
            reply_markup=_checkout_keyboard(checkout.request.id),
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("paid:"))
    async def paid_handler(callback: CallbackQuery, bot: Bot) -> None:
        if callback.data is None:
            return
        try:
            request_id = decode_uuid(callback.data.split(":", 1)[1])
            async with system_project_session_scope(session_factory, project_id) as session:
                await require_awaiting_receipt_for_telegram_user(
                    session,
                    project_id=project_id,
                    request_id=request_id,
                    telegram_user_id=callback.from_user.id,
                )
        except (ValueError, PermissionError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        await callback.answer()
        await bot.send_message(
            chat_id=callback.from_user.id,
            text=(
                f"{RECEIPT_REPLY_PREFIX}{request_id}\n"
                "Ответьте на это сообщение фотографией чека или документом "
                "(PDF/JPG/PNG/WEBP). До получения вложения заявка не будет "
                "отправлена администратору."
            ),
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Прикрепите чек или скриншот",
            ),
        )

    @router.message(ReceiptReplyFilter())
    async def receipt_handler(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        request_id = _receipt_request_id(message, bot_id=bot.id)
        if request_id is None:
            return
        if not _acceptable_receipt(message):
            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "Нужен файл: фото или документ PDF/JPG/PNG/WEBP. "
                    "Ответьте вложением на сообщение с номером заявки."
                ),
            )
            return

        try:
            async with system_project_session_scope(session_factory, project_id) as session:
                submission = await submit_checkout_for_telegram_user(
                    session,
                    project_id=project_id,
                    request_id=request_id,
                    telegram_user_id=message.from_user.id,
                )
        except (ValueError, PermissionError) as exc:
            await bot.send_message(chat_id=message.chat.id, text=str(exc))
            return

        delivered = 0
        for reviewer_id in submission.reviewer_telegram_ids:
            try:
                # Telegram-to-Telegram copy: no file bytes/file IDs are persisted by our app.
                copied = await bot.copy_message(
                    chat_id=reviewer_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                # Reply to this exact receipt so concurrent submissions cannot be mixed up.
                await bot.send_message(
                    chat_id=reviewer_id,
                    text=_review_text(submission.item),
                    reply_markup=_review_keyboard(submission.request.id),
                    reply_parameters=ReplyParameters(message_id=copied.message_id),
                )
                delivered += 1
            except AiogramError:
                # The buyer can reply with the same proof again. /payments recovers
                # review controls without pretending an attachment was delivered.
                continue

        if delivered:
            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "📨 <b>Подтверждение отправлено</b>\n\n"
                    "Администратор проверит поступление средств и сообщит результат."
                ),
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "Заявка зарегистрирована, но доставить документ администратору "
                    "не удалось. Повторите отправку вложения ответом на запрос выше."
                ),
            )

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_handler(callback: CallbackQuery, bot: Bot) -> None:
        if callback.data is None:
            return
        try:
            request_id = decode_uuid(callback.data.split(":", 1)[1])
            async with system_project_session_scope(session_factory, project_id) as session:
                await cancel_checkout_for_telegram_user(
                    session,
                    project_id=project_id,
                    request_id=request_id,
                    telegram_user_id=callback.from_user.id,
                )
        except (ValueError, PermissionError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await callback.answer("Заявка отменена")
        await bot.send_message(
            chat_id=callback.from_user.id,
            text="Заявка на оплату отменена.",
        )

    async def _review_callback(
        callback: CallbackQuery,
        bot: Bot,
        *,
        approve: bool,
    ) -> None:
        if callback.data is None:
            return
        try:
            request_id = decode_uuid(callback.data.split(":", 1)[1])
            async with system_project_session_scope(session_factory, project_id) as session:
                result = await review_payment_request_from_telegram(
                    session,
                    project_id=project_id,
                    request_id=request_id,
                    actor_telegram_user_id=callback.from_user.id,
                    actor_username=callback.from_user.username,
                    actor_first_name=callback.from_user.first_name,
                    approve=approve,
                )
        except PermissionError:
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        if not result.changed:
            await callback.answer("Эта заявка уже обработана", show_alert=True)
            return

        await callback.answer("Оплата подтверждена" if approve else "Оплата отклонена")
        if callback.message is not None:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    reply_markup=None,
                )
            except AiogramError:
                # The decision is committed, so a stale keyboard must not undo it.
                pass

        await _safe_send_message(
            bot,
            callback.from_user.id,
            _admin_review_result_text(result, approve=approve),
            parse_mode="HTML",
        )
        await _safe_send_message(
            bot,
            result.item.customer_telegram_user_id,
            _customer_review_result_text(result, approve=approve),
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("approve:"))
    async def approve_handler(callback: CallbackQuery, bot: Bot) -> None:
        await _review_callback(callback, bot, approve=True)

    @router.callback_query(F.data.startswith("reject:"))
    async def reject_handler(callback: CallbackQuery, bot: Bot) -> None:
        await _review_callback(callback, bot, approve=False)

    @router.message(Command("payments"))
    async def pending_reviews_handler(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        try:
            async with system_project_session_scope(session_factory, project_id) as session:
                items = await list_pending_reviews_for_actor(
                    session,
                    project_id=project_id,
                    actor_telegram_user_id=message.from_user.id,
                    actor_username=message.from_user.username,
                    actor_first_name=message.from_user.first_name,
                )
        except PermissionError:
            await bot.send_message(chat_id=message.from_user.id, text="Недостаточно прав.")
            return

        if not items:
            await bot.send_message(
                chat_id=message.from_user.id,
                text="Заявок на проверку сейчас нет.",
            )
            return

        for item in items:
            await bot.send_message(
                chat_id=message.from_user.id,
                text=_review_text(item),
                reply_markup=_review_keyboard(item.request_id),
            )

    return router
