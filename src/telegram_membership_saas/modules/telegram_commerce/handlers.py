from __future__ import annotations

import inspect
from contextlib import nullcontext
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.localization import (
    current_language,
    recipient_language,
    tr,
)
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.telegram_commerce.account import (
    ACCOUNT_MAX_PAGE,
    account_home_screen,
    account_list_screen,
    load_buyer_account,
    support_screen,
)
from telegram_membership_saas.modules.telegram_commerce.rich_ui import (
    rich_subscription_enabled,
    rich_subscription_offer_html,
    send_rich_subscription_offer,
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
    is_period_only_plan_name,
    list_pending_reviews_for_actor,
    require_awaiting_receipt_for_telegram_user,
    review_payment_request_from_telegram,
    submit_checkout_for_telegram_user,
)
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    buyer_root_screen,
    catalog_screen,
    list_buyer_offers,
    offer_screen,
    rich_catalog_html,
)
from telegram_membership_saas.rich_messages import classic_messages
from telegram_membership_saas.telegram_menus import refresh_user_commands

RECEIPT_REPLY_PREFIX = "Подтверждение оплаты по заявке: "
RECEIPT_REPLY_PREFIX_EN = "Payment confirmation for request: "
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
    for prefix in (RECEIPT_REPLY_PREFIX, RECEIPT_REPLY_PREFIX_EN):
        if header.startswith(prefix):
            try:
                return UUID(header[len(prefix):])
            except ValueError:
                return None
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
                    text=tr("✅ Я оплатил", "✅ I have paid"),
                    style="success",
                    callback_data=f"paid:{token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=tr("Отменить заявку", "Cancel request"),
                    style="danger",
                    callback_data=f"cancel:{token}",
                )
            ],
            [InlineKeyboardButton(
                text=tr("💎 К тарифам", "💎 Browse plans"),
                callback_data="checkoutnav:catalog",
            )],
        ]
    )


def _review_keyboard(request_id: UUID) -> InlineKeyboardMarkup:
    token = encode_uuid(request_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr("✅ Подтвердить", "✅ Approve"),
                    style="success",
                    callback_data=f"approve:{token}",
                ),
                InlineKeyboardButton(
                    text=tr("Отклонить", "Reject"),
                    style="danger",
                    callback_data=f"reject:{token}",
                ),
            ],
            [InlineKeyboardButton(
                text=tr("👤 Покупатель и счёт", "👤 Customer and invoice"),
                callback_data="own:invoice:" + token,
            )],
        ]
    )


def _review_text(item: ReviewItem) -> str:
    username = (f"@{item.customer_username}" if item.customer_username
                else tr("без username", "no username"))
    amount = format_amount(item.amount, 18)
    return (
        tr(
            "🧾 Заявка на подтверждение\n\n"
            f"👤 Покупатель: {username}\n"
            f"📦 Тариф: {item.plan_name}\n"
            f"💳 Оплата: {amount} {item.currency_code}\n"
            f"Способ: {item.payment_method_name}\n"
            f"🔖 Заявка: {item.request_id}\n\n"
            "Проверьте поступление средств в банке, затем примите решение.",
            "🧾 Payment pending review\n\n"
            f"👤 Customer: {username}\n"
            f"📦 Plan: {item.plan_name}\n"
            f"💳 Amount: {amount} {item.currency_code}\n"
            f"Method: {item.payment_method_name}\n"
            f"🔖 Request: {item.request_id}\n\n"
            "Confirm the bank transfer before approving the payment.",
        )
    )


def _subscription_end_text(ends_at: datetime | None, timezone: str) -> str:
    if ends_at is None:
        return tr("Бессрочно", "Lifetime")
    return f"{ends_at.astimezone(ZoneInfo(timezone)):%d.%m.%Y %H:%M} ({timezone})"



def _admin_review_result_text(result: ReviewResult, *, approve: bool) -> str:
    item = result.item
    expiration = escape(
        _subscription_end_text(result.subscription_period_end, result.project_timezone)
    )
    username = f"@{item.customer_username}" if item.customer_username else str(
        item.customer_telegram_user_id
    )
    headline = (
        tr("✅ <b>Оплата подтверждена</b>", "✅ <b>Payment approved</b>")
        if approve else tr("❌ <b>Оплата отклонена</b>", "❌ <b>Payment rejected</b>")
    )
    details = (
        tr("👤 Покупатель: ", "👤 Customer: ") + escape(username) + "\n"
        + tr("📦 Тариф: ", "📦 Plan: ") + escape(item.plan_name) + "\n"
        + tr("💳 Сумма: ", "💳 Amount: ")
        + f"{format_amount(item.amount, 18)} {escape(item.currency_code)}\n"
    )
    if approve:
        details += (
            tr("🟢 Статус подписки: активна\n", "🟢 Subscription: active\n")
            + tr("📅 Действует до: ", "📅 Valid until: ") + expiration + "\n\n"
            + tr("Бот автоматически предоставит доступ покупателю.",
                 "The bot will automatically grant the customer access.")
        )
    else:
        details += tr("\nЗаявка закрыта. Подписка не активирована.",
                      "\nThe request is closed. No subscription was activated.")
    return f"{headline}\n\n{details}"


def _customer_review_result_text(result: ReviewResult, *, approve: bool) -> str:
    expiration = escape(
        _subscription_end_text(result.subscription_period_end, result.project_timezone)
    )
    if approve:
        return (
            tr("✅ <b>Подписка активирована</b>\n\n",
               "✅ <b>Subscription activated</b>\n\n")
            + f"📦 {escape(result.item.plan_name)}\n"
            + tr("📅 До: ", "📅 Until: ") + expiration + "\n\n"
            + tr("🔐 Если вы ещё не в канале, бот пришлёт кнопку для вступления.",
                 "🔐 If you are not in the channel yet, the bot will send a join button.")
        )
    return (
        tr("❌ <b>Оплата отклонена</b>\n\n", "❌ <b>Payment rejected</b>\n\n")
        + tr("Тариф: ", "Plan: ") + escape(result.item.plan_name) + "\n"
        + tr("Если произошла ошибка, свяжитесь с администратором канала.",
             "If you believe this is a mistake, contact the channel administrator.")
    )


def _buyer_error(exc: Exception) -> str:
    """Return safe English UI errors; do not expose unexpected service internals."""
    if current_language() == "ru":
        return str(exc)
    translated = {
        "Бот отключается. Создание новых оплат временно недоступно":
            "This bot is shutting down; new purchases are temporarily unavailable.",
        "Заявка уже отправлена или закрыта":
            "This payment request has already been submitted or closed.",
        "Selected plan price/payment method is no longer available":
            "This plan or payment option is no longer available. Reopen /plans.",
        "Payment request does not belong to this Telegram user":
            "This payment request does not belong to your account.",
        "Project is not active or accessible":
            "This subscription service is currently unavailable.",
        "Payment request is not accessible in the current project scope":
            "This payment request is no longer accessible.",
    }
    return translated.get(str(exc), "Request unavailable. Please reopen /plans and retry.")

async def _safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except AiogramError:
        return


def _account_navigation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=tr("💎 К тарифам", "💎 Browse plans"),
            callback_data="account:catalog",
        )],
    ])


async def _send_screen(
    bot: Bot, *, chat_id: int, text: str,
    keyboard: InlineKeyboardMarkup, message_id: int | None = None,
) -> None:
    """Edit an inline screen; fall back if the old message is not editable."""
    if message_id is not None:
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id,
                parse_mode="HTML", reply_markup=keyboard,
            )
            return
        except AiogramError as exc:
            if "message is not modified" in str(exc).lower():
                return
    await bot.send_message(
        chat_id=chat_id, text=text,
        parse_mode="HTML", reply_markup=keyboard,
    )


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
    notice: str | None = None,
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
    # All catalog and offer cards use editable Rich Messages. Telegram
    # supports editMessageText(rich_message=...), so old buttons disappear
    # instead of piling up above the current screen.
    rich_failed = False
    if (
        notice is None
        and inspect.iscoroutinefunction(getattr(bot, "send_rich_message", None))
        and rich_subscription_enabled(bot.id)
    ):
        rich_html = None
        if price_id is not None:
            selected = next(
                (item for item in offers if item.option.plan_price_id == price_id),
                None,
            )
            if selected is not None:
                rich_html = rich_subscription_offer_html(selected)
        else:
            rich_html = rich_catalog_html(offers, currency=currency, page=page)
        if rich_html is not None:
            if await send_rich_subscription_offer(
                bot, chat_id=chat_id, rich_html=rich_html,
                reply_markup=keyboard, message_id=message_id,
            ):
                return True
            rich_failed = True
    # Do not make a second rejected Rich request on an unsupported endpoint.
    with classic_messages() if rich_failed else nullcontext():
        await _send_screen(
            bot, chat_id=chat_id,
            text=(notice + "\n\n" + text) if notice else text,
            keyboard=keyboard, message_id=message_id,
        )
    return True


async def _send_buyer_start(
    bot: Bot, *, chat_id: int,
    session_factory: async_sessionmaker[AsyncSession], project_id: UUID,
) -> None:
    """Only show file folders if this bot has at least one published good."""
    # Lazy import avoids commerce -> file_sales -> telegram_admin -> commerce cycle.
    from telegram_membership_saas.modules.file_sales.service import list_folders

    folders = await list_folders(
        session_factory, project_id=project_id, bot_id=bot.id,
    )
    if not folders:
        # Restore the pre-file-sales buyer /start exactly: subscription catalog.
        await _send_catalog(
            bot, chat_id=chat_id, session_factory=session_factory,
            project_id=project_id,
        )
        return

    async with system_project_session_scope(session_factory, project_id) as session:
        channel_name = await session.scalar(
            select(Resource.name).where(
                Resource.project_id == project_id,
                Resource.status == ResourceStatus.ACTIVE.value,
            ).order_by(Resource.created_at, Resource.id).limit(1)
        )
    text, keyboard = buyer_root_screen(
        channel_name or tr("Подписки", "Subscriptions"),
        tuple((folder.id, folder.name) for folder in folders),
    )
    await _send_screen(
        bot, chat_id=chat_id, text=text, keyboard=keyboard,
    )


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

    async def refresh_menu(bot: Bot, user_id: int) -> None:
        # An owner may open buyer history/support without losing their admin
        # Telegram command menu. Refresh from the actual current project role.
        await refresh_user_commands(
            bot, session_factory, chat_id=user_id,
            language=current_language(), project_id=project_id,
        )

    @router.message(Command("start", "plans", "menu"))
    async def catalog_handler(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        await refresh_menu(bot, message.from_user.id)
        # Aiogram Command accepts captions on media messages too.
        source = message.text or message.caption or "/start"
        command = source.split(maxsplit=1)[0].split("@")[0].lower()
        if command == "/plans":
            await _send_catalog(
                bot, chat_id=message.from_user.id,
                session_factory=session_factory, project_id=project_id,
            )
        else:
            await _send_buyer_start(
                bot, chat_id=message.from_user.id,
                session_factory=session_factory, project_id=project_id,
            )

    @router.callback_query(F.data == "buyer:channel")
    async def buyer_channel_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not _buyer_screen_callback(callback):
            await callback.answer(
                tr("Откройте личный чат с ботом", "Open your private chat"),
                show_alert=True,
            )
            return
        await _send_catalog(
            bot, chat_id=callback.from_user.id,
            session_factory=session_factory, project_id=project_id,
            message_id=callback.message.message_id,
        )
        await callback.answer()

    async def send_account(
        bot: Bot, *, user_id: int, section: str = "home",
        page: int = 0, message_id: int | None = None,
    ) -> None:
        async with system_project_session_scope(session_factory, project_id) as session:
            account = await load_buyer_account(
                session, project_id=project_id, telegram_user_id=user_id,
                section=section, page=page,
            )
        text, keyboard = (
            account_home_screen(account)
            if section == "home"
            else account_list_screen(account, section=section, page=page)
        )
        await _send_screen(
            bot, chat_id=user_id, text=text, keyboard=keyboard,
            message_id=message_id,
        )

    @router.message(Command("subscriptions", "subscription"))
    async def subscriptions_command(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        await refresh_menu(bot, message.from_user.id)
        await send_account(bot, user_id=message.from_user.id)

    @router.message(Command("support"))
    async def support_command(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        await refresh_menu(bot, message.from_user.id)
        text, keyboard = support_screen()
        await _send_screen(
            bot, chat_id=message.from_user.id, text=text, keyboard=keyboard,
        )

    @router.message(Command("cancel"))
    async def buyer_exit_command(message: Message, bot: Bot) -> None:
        """Leaving the UI must never implicitly cancel a payment request."""
        if message.from_user is None or message.chat.type != "private":
            return
        await refresh_menu(bot, message.from_user.id)
        await send_account(bot, user_id=message.from_user.id)

    @router.callback_query(F.data.startswith("checkoutnav:"))
    async def checkout_navigation_callback(callback: CallbackQuery, bot: Bot) -> None:
        """Preserve the original payment instructions and cancel controls.

        These callbacks are used ONLY on already-created checkout messages.
        Sending a new screen (rather than editing the payment message) lets
        buyers return to the original instructions without creating new requests.
        """
        if not _buyer_screen_callback(callback):
            await callback.answer(
                tr("Откройте личный чат с ботом", "Open a private chat with this bot"),
                show_alert=True,
            )
            return
        action = (callback.data or "").removeprefix("checkoutnav:")
        if action == "home":
            await send_account(bot, user_id=callback.from_user.id)
        elif action == "catalog":
            await _send_catalog(
                bot, chat_id=callback.from_user.id,
                session_factory=session_factory, project_id=project_id,
            )
        else:
            await callback.answer(
                tr("Кнопка устарела", "This button has expired"),
                show_alert=True,
            )
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("account:"))
    async def account_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not _buyer_screen_callback(callback):
            await callback.answer(
                tr("Откройте личный чат с ботом", "Open a private chat with this bot"),
                show_alert=True,
            )
            return
        action = (callback.data or "")[8:]
        try:
            if action == "home":
                await send_account(
                    bot, user_id=callback.from_user.id,
                    message_id=callback.message.message_id,
                )
            elif action == "catalog":
                await _send_catalog(
                    bot, chat_id=callback.from_user.id,
                    session_factory=session_factory, project_id=project_id,
                    message_id=callback.message.message_id,
                )
            elif action == "support":
                text, keyboard = support_screen()
                await _send_screen(
                    bot, chat_id=callback.from_user.id, text=text,
                    keyboard=keyboard, message_id=callback.message.message_id,
                )
            else:
                section, page_token = action.split(":", 1)
                if section not in ("subs", "reqs") or (
                    not page_token.isascii() or not page_token.isdecimal()
                    or int(page_token) > ACCOUNT_MAX_PAGE
                ):
                    raise ValueError("Invalid account page")
                await send_account(
                    bot, user_id=callback.from_user.id,
                    section=section, page=int(page_token),
                    message_id=callback.message.message_id,
                )
        except ValueError:
            await callback.answer(
                tr("Экран устарел. Откройте /subscriptions.",
                   "This screen has expired. Open /subscriptions."),
                show_alert=True,
            )
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("cat:"))
    async def currency_handler(callback: CallbackQuery, bot: Bot) -> None:
        if not _buyer_screen_callback(callback):
            await callback.answer(
                tr("Откройте личный чат с ботом", "Open your private chat with the bot"),
                show_alert=True,
            )
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
                tr("Список изменился. Откройте /plans заново.",
                   "The catalog changed. Reopen /plans."),
                show_alert=True,
            )
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("offer:"))
    async def offer_handler(callback: CallbackQuery, bot: Bot) -> None:
        if not _buyer_screen_callback(callback):
            await callback.answer(
                tr("Откройте личный чат с ботом", "Open your private chat with the bot"),
                show_alert=True,
            )
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
                    tr("Тариф больше недоступен. Откройте /plans заново.",
                       "This plan is no longer available. Reopen /plans."),
                    show_alert=True,
                )
                return
        except ValueError:
            await callback.answer(
                tr("Тариф больше недоступен. Откройте /plans заново.",
                   "This plan is no longer available. Reopen /plans."),
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
            await callback.answer(_buyer_error(exc), show_alert=True)
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
                tr("💎 <b>Оформление подписки</b>\n\n",
                   "💎 <b>Subscription checkout</b>\n\n")
                + (
                    ""
                    if is_period_only_plan_name(
                        checkout.option.plan_name,
                        checkout.option.duration_value,
                        checkout.option.duration_unit,
                    )
                    else f"📦 <b>{escape(checkout.option.plan_name)}</b>\n"
                )
                + f"⏳ {period}\n"
                + f"💳 <b>{amount} {escape(checkout.request.currency_code)}</b>\n"
                + tr("Способ: ", "Method: ")
                + f"{escape(checkout.option.payment_method_name)}\n\n"
                + f"{escape(checkout.instruction)}\n\n"
                + tr("После оплаты нажмите <b>«Я оплатил»</b> и прикрепите чек. "
                     "Поступление проверит администратор.",
                     "After paying, tap <b>“I have paid”</b> and attach your receipt. "
                     "The channel administrator will verify your payment.")
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
            await callback.answer(_buyer_error(exc), show_alert=True)
            return

        await callback.answer()
        await bot.send_message(
            chat_id=callback.from_user.id,
            text=(
                tr(RECEIPT_REPLY_PREFIX, RECEIPT_REPLY_PREFIX_EN)
                + f"{request_id}\n"
                + tr("Ответьте на это сообщение фотографией чека или документом "
                     "(PDF/JPG/PNG/WEBP). До получения вложения заявка не будет "
                     "отправлена администратору.",
                     "Reply to this message with a receipt photo or a document "
                     "(PDF/JPG/PNG/WEBP). Your payment will not be sent for review "
                     "until the attachment is received.")
                + tr(
                    "\n\nМожно выйти через /menu. Заявка останется "
                    "в разделе /subscriptions.",
                    "\n\nYou can leave via /menu. Your pending request "
                    "stays in /subscriptions.",
                )
            ),
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder=tr("Прикрепите чек или скриншот",
                                           "Attach receipt or screenshot"),
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
                    tr("Нужен файл: фото или документ PDF/JPG/PNG/WEBP. "
                       "Ответьте вложением на сообщение с номером заявки.",
                       "A receipt photo or PDF/JPG/PNG/WEBP document is required. "
                       "Reply to the payment request with an attachment.")
                ),
                reply_markup=_account_navigation_keyboard(),
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
            await bot.send_message(chat_id=message.chat.id, text=_buyer_error(exc))
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
                async with recipient_language(session_factory, reviewer_id):
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
                    tr("📨 <b>Подтверждение отправлено</b>\n\n"
                       "Администратор проверит поступление средств и сообщит результат.",
                       "📨 <b>Receipt submitted</b>\n\n"
                       "The administrator will verify your payment and notify you.")
                ),
                parse_mode="HTML",
                reply_markup=_account_navigation_keyboard(),
            )
        else:
            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    tr("Заявка зарегистрирована, но доставить документ администратору "
                       "не удалось. Повторите отправку вложения ответом на запрос выше.",
                       "Your payment request was recorded, but the receipt could not be "
                       "delivered to the administrator. Reply with the attachment again.")
                ),
                reply_markup=_account_navigation_keyboard(),
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
            await callback.answer(_buyer_error(exc), show_alert=True)
            return
        await callback.answer(tr("Заявка отменена", "Payment request canceled"))
        # Return directly to the actual buyer home (catalog), not account
        # history. Only the canceled request is modified.
        await _send_catalog(
            bot, chat_id=callback.from_user.id,
            session_factory=session_factory, project_id=project_id,
            notice=tr("✅ Заявка на оплату отменена.",
                      "✅ Your payment request was canceled."),
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
            await callback.answer(tr("Недостаточно прав", "Not authorized"), show_alert=True)
            return
        except ValueError as exc:
            await callback.answer(_buyer_error(exc), show_alert=True)
            return

        if not result.changed:
            await callback.answer(tr("Эта заявка уже обработана", "Request already reviewed"),
                                  show_alert=True)
            return

        await callback.answer(
            tr("Оплата подтверждена", "Payment approved") if approve
            else tr("Оплата отклонена", "Payment rejected"),
        )
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
        async with recipient_language(
            session_factory, result.item.customer_telegram_user_id,
        ):
            await _safe_send_message(
                bot,
                result.item.customer_telegram_user_id,
                _customer_review_result_text(result, approve=approve),
                parse_mode="HTML",
                reply_markup=_account_navigation_keyboard(),
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
            await bot.send_message(
                chat_id=message.from_user.id,
                text=tr("Недостаточно прав.", "Not authorized."),
            )
            return

        if not items:
            await bot.send_message(
                chat_id=message.from_user.id,
                text=tr("Заявок на проверку сейчас нет.",
                        "There are no pending payment reviews."),
            )
            return

        for item in items:
            await bot.send_message(
                chat_id=message.from_user.id,
                text=_review_text(item),
                reply_markup=_review_keyboard(item.request_id),
            )

    return router
