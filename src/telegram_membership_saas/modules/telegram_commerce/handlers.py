from __future__ import annotations

from uuid import UUID

from aiogram import Bot, F, Router
from aiogram.exceptions import AiogramError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.telegram_commerce.service import (
    CheckoutOption,
    ReviewItem,
    cancel_checkout_for_telegram_user,
    create_checkout_for_telegram_user,
    decode_uuid,
    encode_uuid,
    format_amount,
    format_period,
    list_checkout_options,
    list_pending_reviews_for_actor,
    review_payment_request_from_telegram,
    submit_checkout_for_telegram_user,
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
                    callback_data=f"paid:{token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
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
                    callback_data=f"approve:{token}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{token}",
                ),
            ]
        ]
    )


def _review_text(item: ReviewItem) -> str:
    username = f"@{item.customer_username}" if item.customer_username else "без username"
    amount = format_amount(item.amount, 18)
    return (
        "Новая заявка на оплату\n"
        f"Покупатель: {username} · Telegram ID {item.customer_telegram_user_id}\n"
        f"Тариф: {item.plan_name}\n"
        f"Сумма: {amount} {item.currency_code}\n"
        f"Способ: {item.payment_method_name}\n"
        f"Заявка: {item.request_id}"
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
) -> None:
    async with system_project_session_scope(session_factory, project_id) as session:
        options = await list_checkout_options(session, project_id=project_id)

    if not options:
        await bot.send_message(
            chat_id=chat_id,
            text="Сейчас нет доступных тарифов. Попробуйте позже.",
        )
        return

    rows = [
        [
            InlineKeyboardButton(
                text=_option_label(option),
                callback_data=(
                    f"buy:{encode_uuid(option.plan_price_id)}:"
                    f"{encode_uuid(option.payment_method_id)}"
                ),
            )
        ]
        for option in options
    ]
    await bot.send_message(
        chat_id=chat_id,
        text="Выберите тариф, валюту и способ оплаты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
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
        await bot.send_message(
            chat_id=callback.from_user.id,
            text=(
                f"Тариф: {checkout.option.plan_name}\n"
                f"Сумма: {amount} {checkout.request.currency_code}\n"
                f"Способ: {checkout.option.payment_method_name}\n\n"
                f"{checkout.instruction}\n\n"
                "После перевода нажмите «Я оплатил». "
                "Подтверждение выполняется администратором после проверки поступления."
            ),
            reply_markup=_checkout_keyboard(checkout.request.id),
        )

    @router.callback_query(F.data.startswith("paid:"))
    async def paid_handler(callback: CallbackQuery, bot: Bot) -> None:
        if callback.data is None:
            return
        try:
            request_id = decode_uuid(callback.data.split(":", 1)[1])
            async with system_project_session_scope(session_factory, project_id) as session:
                submission = await submit_checkout_for_telegram_user(
                    session,
                    project_id=project_id,
                    request_id=request_id,
                    telegram_user_id=callback.from_user.id,
                )
        except (ValueError, PermissionError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        await callback.answer("Заявка отправлена")
        await bot.send_message(
            chat_id=callback.from_user.id,
            text="Заявка отправлена администратору на проверку оплаты.",
        )
        if submission.newly_submitted:
            text = _review_text(submission.item)
            for reviewer_id in submission.reviewer_telegram_ids:
                await _safe_send_message(
                    bot,
                    reviewer_id,
                    text,
                    reply_markup=_review_keyboard(submission.request.id),
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

        status_text = "Оплата подтверждена" if approve else "Оплата отклонена"
        await callback.answer(status_text)
        if not result.changed:
            return

        if approve:
            customer_text = (
                "Оплата подтверждена ✅\n"
                "Доступ готовится автоматически. Если требуется вступление, "
                "бот пришлёт защищённую ссылку."
            )
        else:
            customer_text = (
                "Оплата отклонена ❌\n"
                "Если это ошибка, свяжитесь с администратором проекта."
            )
        await _safe_send_message(
            bot,
            result.item.customer_telegram_user_id,
            customer_text,
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
