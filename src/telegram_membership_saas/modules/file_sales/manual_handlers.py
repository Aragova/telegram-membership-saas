"""Connected-bot file sales checkout: existing manual methods + optional Stars."""
from __future__ import annotations

import logging
from html import escape
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas import receipt_intents
from telegram_membership_saas.localization import recipient_language, tr
from telegram_membership_saas.manual_checkout_ui import (
    manual_checkout_rich_html,
    retire_cancelled_checkout,
    send_manual_checkout,
)
from telegram_membership_saas.modules.file_sales.manual_service import (
    available_methods,
    close_pending_manual_order,
    create_manual_order,
    load_manual_review,
    money,
    pending_manual_reviews,
    price_editor_options,
    require_buyer_order,
    review_manual_order,
    set_manual_price,
    submit_manual_receipt,
)
from telegram_membership_saas.modules.file_sales.payment_controls import (
    file_stars_enabled,
)
from telegram_membership_saas.modules.file_sales.rich_ui import (
    rich_file_card_html,
    send_file_card,
)
from telegram_membership_saas.modules.file_sales.service import (
    mark_file_delivery,
    purchased_assets,
    view_product,
)
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.telegram_commerce.service import decode_uuid, encode_uuid
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    CURRENCY_ICONS,
    CURRENCY_ORDER,
)
from telegram_membership_saas.payment_review_ui import (
    rich_file_close_warning,
    rich_file_review,
    rich_file_review_result,
    rich_file_reviews_queue,
    send_payment_review,
)
from telegram_membership_saas.receipt_prompt_ui import send_receipt_prompt

log = logging.getLogger(__name__)
RECEIPT_PREFIX = "FILE_PAYMENT_RECEIPT:"
ENTRY_SECONDS = 900


def _short_amount(value) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _button(label: str, callback: str, style: str = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data="fm:" + callback, style=style)


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _receipt_order_id(message: Message, bot_id: int) -> UUID | None:
    if message.chat.type != "private" or not message.reply_to_message:
        return None
    original = message.reply_to_message
    if original.from_user is None or original.from_user.id != bot_id:
        return None
    first = (original.text or "").split("\n", 1)[0]
    if not first.startswith(RECEIPT_PREFIX):
        return None
    try:
        return UUID(first[len(RECEIPT_PREFIX):])
    except ValueError:
        return None


class ManualReceiptFilter(BaseFilter):
    async def __call__(self, message: Message, bot: Bot) -> bool:
        if message.text and message.text.startswith("/"):
            return False
        return _receipt_order_id(message, bot.id) is not None


class ActiveFileReceiptFilter(BaseFilter):
    """Normal private uploads only for a selected pending file purchase."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        return bool(
            message.chat.type == "private"
            and message.from_user is not None
            and not message.reply_to_message
            and (message.photo or message.document)
            and receipt_intents.peek(
                bot.id, message.from_user.id, kind="file",
            ) is not None
        )


class ManualPriceFilter(BaseFilter):
    def __init__(self, pending: dict[int, dict]):
        self.pending = pending

    async def __call__(self, message: Message) -> bool:
        if message.chat.type != "private" or message.from_user is None:
            return False
        if getattr(message, "successful_payment", None) is not None:
            return False
        if not message.text or message.text.startswith("/"):
            return False
        entry = self.pending.get(message.from_user.id)
        if entry is None:
            return False
        if entry["expires"] <= monotonic():
            self.pending.pop(message.from_user.id, None)
            return False
        return True


async def show_file_payment_options(
    bot: Bot, *, factory: async_sessionmaker[AsyncSession],
    project_id: UUID, buyer_id: int, product_id: UUID,
    currency: str | None = None,
) -> None:
    product, count = await view_product(
        factory, project_id=project_id, bot_id=bot.id, product_id=product_id,
    )
    if not count:
        raise ValueError(tr("У товара пока нет вложений", "This product has no attachments yet"))
    options = await available_methods(
        factory, project_id=project_id, bot_id=bot.id,
        product_id=product_id, buyer_id=buyer_id,
    )
    stars_enabled = await file_stars_enabled(
        factory, project_id=project_id, bot_id=bot.id,
    )
    if not options and (product.stars_price is None or not stars_enabled):
        await bot.send_message(
            buyer_id, tr(
                "⏸️ Способы оплаты этого файла временно отключены.",
                "⏸️ Payment methods for this file are temporarily disabled.",
            ),
            reply_markup=_keyboard([[InlineKeyboardButton(
                text=tr("⬅️ Товары", "⬅️ Products"),
                callback_data="fs:folder:" + encode_uuid(product.folder_id),
            )]]),
        )
        return
    codes = tuple(sorted({option.currency for option in options}, key=lambda c: (
        CURRENCY_ORDER.index(c) if c in CURRENCY_ORDER else 99, c,
    )))
    chosen = currency if currency in codes else (codes[0] if codes else None)
    code = encode_uuid(product_id)
    rows: list[list[InlineKeyboardButton]] = []
    if codes:
        buttons = [
            _button(
                ("🟢" if c == chosen else "") + CURRENCY_ICONS.get(c, "🌐") + c,
                "currency:" + code + ":" + c,
                "success" if c == chosen else "primary",
            ) for c in codes
        ]
        rows.extend(buttons[i:i + 5] for i in range(0, len(buttons), 5))
    for option in options:
        if option.currency != chosen:
            continue
        rows.append([_button(
            "💳 " + option.method_name[:36],
            "method:" + code + ":" + chosen + ":" + encode_uuid(option.method_id),
        )])
    if product.stars_price is not None and stars_enabled:
        rows.append([InlineKeyboardButton(
            text=tr("⭐ Звёзды Telegram · ", "⭐ Telegram Stars · ")
            + str(product.stars_price) + " XTR",
            callback_data="fs:buy:" + code,
            style="success",
        )])
    rows.append([InlineKeyboardButton(
        text=tr("⬅️ Товары", "⬅️ Products"),
        callback_data="fs:folder:" + encode_uuid(product.folder_id),
    )])
    price_label = (
        next((money(x.amount, x.precision) + " " + chosen
              for x in options if x.currency == chosen), "")
        if chosen else str(product.stars_price) + " ⭐"
    )
    legacy_html = (
        tr("💎 <b>Оформление покупки файла</b>\n\n",
           "💎 <b>File purchase checkout</b>\n\n")
        + "📦 <b>" + escape(product.name) + "</b>\n"
        + (escape(product.description) + "\n" if getattr(
            product, "description", None,
        ) else "")
        + tr("💰 Цена: ", "💰 Price: ") + escape(price_label) + "\n\n"
        + tr("👇 <b>Как вам удобнее оплатить?</b>",
             "👇 <b>How would you like to pay?</b>")
    )
    await send_file_card(
        bot, chat_id=buyer_id,
        rich_html=rich_file_card_html(
            product_name=product.name,
            description=getattr(product, "description", None),
            price=price_label, file_count=count,
        ),
        legacy_html=legacy_html, reply_markup=_keyboard(rows),
    )


async def show_manual_prices(
    bot: Bot, *, factory: async_sessionmaker[AsyncSession],
    project_id: UUID, actor_id: UUID, user_id: int, product_id: UUID,
) -> None:
    options = await price_editor_options(
        factory, project_id=project_id, bot_id=bot.id,
        actor_id=actor_id, product_id=product_id,
    )
    code = encode_uuid(product_id)
    rows = [[_button(
        CURRENCY_ICONS.get(currency, "💱") + " " + currency
        + (" · " + _short_amount(amount) if amount is not None else " · ➕"),
        "setprice:" + code + ":" + currency,
        "success" if amount is not None else "primary",
    )] for currency, amount in options]
    rows.append([InlineKeyboardButton(
        text=tr("⬅️ Карточка товара", "⬅️ Product"),
        callback_data="fs:ap:" + code,
    )])
    await bot.send_message(
        user_id, tr(
            "💱 <b>Цены файла в валютах</b>\n"
            "Нажмите валюту и укажите цену. Здесь доступны только валюты "
            "активных способов оплаты. Покупателям показываются только "
            "валюты с указанной ценой.\n\n"
            "Старые покупки сохраняют цену на момент оформления.",
            "💱 <b>File prices by currency</b>\n"
            "Tap a currency to set its price. Only active payment-method "
            "currencies appear, and only priced currencies are shown "
            "to customers.\n\nExisting purchase amounts stay frozen.",
        ),
        parse_mode="HTML", reply_markup=_keyboard(rows),
    )


async def _send_paid_files(
    bot: Bot, *, factory: async_sessionmaker[AsyncSession],
    project_id: UUID, buyer_id: int, purchase_id: UUID,
) -> None:
    order, assets = await purchased_assets(
        factory, project_id=project_id, bot_id=bot.id,
        buyer_id=buyer_id, purchase_id=purchase_id,
    )
    await bot.send_message(
        buyer_id, tr("✅ Ваша покупка: ", "✅ Your purchase: ") + escape(order.product_name),
        parse_mode="HTML",
    )
    for asset in assets:
        method = {
            "document": bot.send_document, "photo": bot.send_photo,
            "video": bot.send_video, "audio": bot.send_audio,
            "animation": bot.send_animation,
        }[asset.kind]
        await method(buyer_id, asset.file_id)
    await mark_file_delivery(
        factory, project_id=project_id, bot_id=bot.id,
        buyer_id=buyer_id, purchase_id=purchase_id,
    )
    await bot.send_message(
        buyer_id, tr("📥 Получить файлы снова можно через /myfiles.",
                     "📥 Download your files again with /myfiles."),
        reply_markup=_keyboard([[_button(
            tr("📥 Получить повторно", "📥 Redownload"),
            "get:" + encode_uuid(purchase_id),
        )]]),
    )


def create_manual_file_router(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
) -> Router:
    router = Router(name="manual-file-checkout")
    editor: dict[int, dict] = {}

    @router.message(Command("filepayments"))
    async def file_payment_reviews_command(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        actor = await ensure_telegram_actor(
            factory, telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        try:
            total, _ = await pending_manual_reviews(
                factory, project_id=project_id, bot_id=bot.id,
                actor_id=actor, page=0,
            )
        except PermissionError:
            await bot.send_message(
                message.chat.id, tr("Недостаточно прав.", "Not authorized."),
            )
            return
        await bot.send_message(
            message.chat.id,
            tr("🧾 Заявки на оплату файлов: ", "🧾 File payment reviews: ")
            + str(total),
            reply_markup=_keyboard([[
                _button(tr("📋 Открыть заявки", "📋 Open reviews"),
                        "reviews", "success"),
            ]]),
        )

    @router.callback_query(F.data.startswith("fm:"))
    async def file_manual_callback(query: CallbackQuery, bot: Bot) -> None:
        if (query.message is None or query.from_user is None
                or query.message.chat.type != "private"
                or query.message.chat.id != query.from_user.id):
            await query.answer()
            return
        uid = query.from_user.id
        action = (query.data or "")[3:]
        try:
            if action.startswith("currency:"):
                _, token, currency = action.split(":")
                await show_file_payment_options(
                    bot, factory=factory, project_id=project_id, buyer_id=uid,
                    product_id=decode_uuid(token), currency=currency,
                )
            elif action.startswith("method:"):
                _, token, currency, method = action.split(":")
                order, paid = await create_manual_order(
                    factory, project_id=project_id, bot_id=bot.id,
                    buyer_id=uid, product_id=decode_uuid(token),
                    currency=currency, method_id=decode_uuid(method),
                )
                if paid:
                    await _send_paid_files(
                        bot, factory=factory, project_id=project_id,
                        buyer_id=uid, purchase_id=order.id,
                    )
                elif order.status == "submitted":
                    await bot.send_message(uid, tr(
                        "📨 Чек уже отправлен администратору. Дождитесь проверки.",
                        "📨 Your receipt has been submitted for review.",
                    ))
                else:
                    amount = _short_amount(order.amount)
                    guidance = tr(
                        "После перевода нажмите «Я оплатил» и прикрепите чек. "
                        "Администратор проверит поступление денег.",
                        "After paying, tap “I have paid” and attach a receipt. "
                        "An administrator will verify the transfer.",
                    )
                    # Retain legacy text and the same payment/cancel callbacks.
                    legacy_html = (
                        tr("💎 <b>Оформление покупки файла</b>\n",
                           "💎 <b>File purchase checkout</b>\n")
                        + "📦 <b>" + escape(order.product_name) + "</b>\n"
                        + "💰 <b>" + escape(amount)
                        + " " + escape(order.currency_code) + "</b>\n"
                        + tr("💳 Способ: ", "💳 Method: ")
                        + escape(order.payment_method_name) + "\n\n"
                        + escape(order.instruction) + "\n\n"
                        + guidance
                    )
                    await send_manual_checkout(
                        bot, chat_id=uid,
                        rich_html=manual_checkout_rich_html(
                            kind="file", name=order.product_name, period=None,
                            amount=amount, currency=order.currency_code,
                            payment_method=order.payment_method_name,
                            instruction=order.instruction, guidance_html=guidance,
                        ),
                        legacy_html=legacy_html, instruction=order.instruction,
                        reply_markup=_keyboard([
                            [_button(tr("✅ Я оплатил", "✅ I have paid"),
                                     "paid:" + encode_uuid(order.id), "success")],
                            [_button(tr("❌ Отменить", "❌ Cancel"),
                                     "cancel:" + encode_uuid(order.id), "danger")],
                        ]),
                    )
            elif action.startswith("paid:"):
                order_id = decode_uuid(action.split(":")[1])
                order = await require_buyer_order(
                    factory, project_id=project_id, bot_id=bot.id,
                    buyer_id=uid, order_id=order_id,
                )
                if order.status != "pending":
                    raise ValueError(tr(
                        "Заявка уже отправлена или закрыта",
                        "This request was already submitted or closed",
                    ))
                await send_receipt_prompt(
                    bot, buyer_id=uid, kind="file", request_id=order.id,
                    cancel_data="fm:receiptstop:" + encode_uuid(order.id),
                    name=order.product_name, amount=_short_amount(order.amount),
                    currency=order.currency_code,
                )
                receipt_intents.arm(
                    bot.id, uid, kind="file", request_id=order.id,
                )
            elif action.startswith("receiptstop:"):
                try:
                    stopping_id = decode_uuid(action.split(":", 1)[1])
                except (ValueError, IndexError):
                    raise ValueError(tr("Кнопка устарела", "Button expired")) from None
                receipt_intents.clear(
                    bot.id, uid, kind="file", request_id=stopping_id,
                )
            elif action.startswith("cancel:"):
                # Buyer may cancel only before submitting proof.
                from telegram_membership_saas.modules.file_sales.manual_service import (
                    cancel_manual_order,
                )
                await cancel_manual_order(
                    factory, project_id=project_id, bot_id=bot.id,
                    buyer_id=uid, order_id=decode_uuid(action.split(":")[1]),
                )
                receipt_intents.clear(
                    bot.id, uid, kind="file",
                    request_id=decode_uuid(action.split(":")[1]),
                )
                # Retire only this successfully canceled checkout card.
                # Original file instructions stay unchanged in the message.
                await retire_cancelled_checkout(bot, callback=query)
                await bot.send_message(uid, tr("Заявка отменена.", "Order canceled."))
            elif action.startswith("prices:") or action.startswith("setprice:"):
                actor = await ensure_telegram_actor(
                    factory, telegram_id=uid,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                parts = action.split(":")
                product_id = decode_uuid(parts[1])
                if parts[0] == "prices":
                    await show_manual_prices(
                        bot, factory=factory, project_id=project_id,
                        actor_id=actor, user_id=uid, product_id=product_id,
                    )
                else:
                    currency = parts[2]
                    await price_editor_options(
                        factory, project_id=project_id, bot_id=bot.id,
                        product_id=product_id, actor_id=actor,
                    )
                    editor[uid] = {
                        "product": product_id, "currency": currency,
                        "expires": monotonic() + ENTRY_SECONDS,
                    }
                    await bot.send_message(
                        uid, tr("💰 Укажите цену в ", "💰 Enter price in ")
                        + currency + tr(" следующим сообщением. /menu — отмена.",
                                        " in your next message. /menu cancels."),
                    )
            elif action == "reviews" or action.startswith("reviews:"):
                token = action.split(":", 1)[1] if ":" in action else "0"
                if not token.isascii() or not token.isdecimal():
                    raise ValueError(tr("Неверная страница", "Invalid page"))
                page = int(token)
                actor = await ensure_telegram_actor(
                    factory, telegram_id=uid,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                total, items = await pending_manual_reviews(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=actor, page=page,
                )
                rows = [[_button(
                    ("🕓 " if item.status == "pending" else "📨 ")
                    + item.product_name[:26] + " · "
                    + _short_amount(item.amount) + " " + item.currency_code,
                    "review:" + encode_uuid(item.id),
                )] for item in items]
                navigation = []
                if page > 0:
                    navigation.append(_button(
                        tr("⬅️ Назад", "⬅️ Back"), "reviews:" + str(page - 1),
                    ))
                if (page + 1) * 8 < total:
                    navigation.append(_button(
                        tr("Далее ➡️", "Next ➡️"), "reviews:" + str(page + 1),
                    ))
                if navigation:
                    rows.append(navigation)
                await send_payment_review(
                    bot, chat_id=uid,
                    rich_html=rich_file_reviews_queue(
                        items, page=page, total=total,
                    ),
                    legacy_text=(
                        tr("🧾 <b>Заявки на оплату файлов</b>",
                           "🧾 <b>File payment reviews</b>")
                        + ("\n" + tr("Нет новых заявок.", "No pending reviews.")
                           if not total else "\n" + tr("Страница ", "Page ")
                           + str(page + 1) + " / " + str((total + 7) // 8))
                    ),
                    parse_mode="HTML", reply_markup=_keyboard(rows),
                )
            elif action.startswith("review:"):
                actor = await ensure_telegram_actor(
                    factory, telegram_id=uid,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                order_id = decode_uuid(action.split(":")[1])
                order = await load_manual_review(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=actor, order_id=order_id,
                )
                if order.receipt_kind == "photo":
                    await bot.send_photo(uid, order.receipt_file_id)
                elif order.receipt_kind == "document":
                    await bot.send_document(uid, order.receipt_file_id)
                await send_payment_review(
                    bot, chat_id=uid,
                    rich_html=rich_file_review(
                        order, receipt_attached=bool(order.receipt_file_id),
                    ),
                    legacy_text=(
                        "📦 <b>" + escape(order.product_name) + "</b>\n"
                        + "💰 " + escape(_short_amount(order.amount)) + " "
                        + escape(order.currency_code) + "\n"
                        + "💳 " + escape(order.payment_method_name)
                    ),
                    parse_mode="HTML",
                    reply_markup=_keyboard(
                        [[_button(
                            tr("⚠️ Закрыть неоплаченный счёт",
                               "⚠️ Close unpaid invoice"),
                            "closeconfirm:" + encode_uuid(order.id), "danger",
                        )]] if order.status == "pending" else
                        [[_button(
                            tr("✅ Подтвердить", "✅ Approve"),
                            "approve:" + encode_uuid(order.id), "success",
                        ), _button(
                            tr("❌ Отклонить", "❌ Reject"),
                            "reject:" + encode_uuid(order.id), "danger",
                        )]],
                    ),
                )
            elif action.startswith("closeconfirm:"):
                actor = await ensure_telegram_actor(
                    factory, telegram_id=uid,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                order_id = decode_uuid(action.split(":")[1])
                order = await load_manual_review(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=actor, order_id=order_id,
                )
                if order.status != "pending":
                    raise ValueError(tr(
                        "Счёт уже изменился или недоступен",
                        "This invoice has changed or is unavailable",
                    ))
                await send_payment_review(
                    bot, chat_id=uid,
                    rich_html=rich_file_close_warning(order),
                    legacy_text=tr(
                        "⚠️ <b>Закрытие неоплаченного счёта</b>\n\n"
                        "Перед закрытием проверьте поступления на банковском счёте: "
                        "покупатель мог перевести деньги, но не прислать чек. "
                        "После закрытия прежние реквизиты станут недействительны, "
                        "а покупателю придёт уведомление.",
                        "⚠️ <b>Close unpaid invoice</b>\n\n"
                        "First verify your bank: the buyer may have transferred "
                        "money without sending a receipt. The old instructions "
                        "will be invalidated and the buyer will be notified.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_keyboard([
                        [_button(tr("⛔ Подтвердить закрытие", "⛔ Confirm closure"),
                                 "close:" + encode_uuid(order_id), "danger")],
                        [_button(tr("⬅️ Назад", "⬅️ Back"), "reviews")],
                    ]),
                )
            elif action.startswith("close:"):
                actor = await ensure_telegram_actor(
                    factory, telegram_id=uid,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                order = await close_pending_manual_order(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=actor, order_id=decode_uuid(action.split(":")[1]),
                )
                await bot.send_message(
                    uid, tr("✅ Неоплаченный счёт закрыт.",
                           "✅ Unpaid invoice closed."),
                )
                try:
                    async with recipient_language(factory, order.telegram_user_id):
                        await bot.send_message(
                            order.telegram_user_id,
                            tr(
                                "⚠️ Счёт на покупку файла отменён владельцем. "
                                "Не переводите деньги по старым реквизитам. "
                                "Если уже оплатили, свяжитесь с поддержкой.",
                                "⚠️ Your file invoice was cancelled by the owner. "
                                "Do not pay the old details. If you've already "
                                "sent money, contact support.",
                            ),
                        )
                except AiogramError:
                    log.warning("Unable to notify buyer of closed file invoice")
            elif action.startswith(("approve:", "reject:")):
                approve = action.startswith("approve:")
                actor = await ensure_telegram_actor(
                    factory, telegram_id=uid,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                )
                order, changed = await review_manual_order(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=actor, order_id=decode_uuid(action.split(":")[1]),
                    approve=approve,
                )
                await send_payment_review(
                    bot, chat_id=uid,
                    rich_html=rich_file_review_result(order, approve=approve),
                    legacy_text=tr(
                        "✅ Оплата подтверждена." if approve
                        else "❌ Заявка отклонена.",
                        "✅ Payment approved." if approve
                        else "❌ Payment rejected.",
                    ),
                )
                if changed:
                    try:
                        async with recipient_language(factory, order.telegram_user_id):
                            await bot.send_message(
                                order.telegram_user_id,
                                tr("✅ Оплата файла подтверждена. Получаем файлы.",
                                   "✅ File payment approved. Sending your files.")
                                if approve else
                                tr("❌ Оплата файла отклонена администратором.",
                                   "❌ File payment rejected by the administrator."),
                            )
                            if approve:
                                await _send_paid_files(
                                    bot, factory=factory, project_id=project_id,
                                    buyer_id=order.telegram_user_id,
                                    purchase_id=order.id,
                                )
                    except AiogramError:
                        log.exception("Manual file payment notice or delivery failed")
                        # Confirmed purchase is durable; /myfiles can redeliver.
            elif action.startswith("get:"):
                await _send_paid_files(
                    bot, factory=factory, project_id=project_id,
                    buyer_id=uid, purchase_id=decode_uuid(action.split(":")[1]),
                )
            else:
                raise ValueError(tr("Неизвестное действие", "Unknown action"))
            await query.answer()
        except (ValueError, PermissionError) as exc:
            await query.answer(str(exc)[:190], show_alert=True)
        except AiogramError:
            log.exception("Manual file Telegram callback failed")
            await query.answer(tr("Ошибка Telegram", "Telegram error"), show_alert=True)

    @router.message(ManualReceiptFilter())
    async def manual_receipt(message: Message, bot: Bot) -> None:
        # Old reply-based messages continue to work for existing invoices.
        order_id = _receipt_order_id(message, bot.id)
        if order_id is not None:
            await _process_manual_receipt(message, bot, order_id, direct=False)

    @router.message(ActiveFileReceiptFilter())
    async def direct_file_receipt(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        order_id = receipt_intents.consume(
            bot.id, message.from_user.id, kind="file",
        )
        if order_id is not None:
            await _process_manual_receipt(message, bot, order_id, direct=True)

    async def _process_manual_receipt(
        message: Message, bot: Bot, order_id: UUID, *, direct: bool,
    ) -> None:
        if message.from_user is None:
            return
        if message.photo:
            kind, file_id = "photo", message.photo[-1].file_id
        elif (message.document and message.document.mime_type in (
            "application/pdf", "image/jpeg", "image/png", "image/webp",
        )):
            kind, file_id = "document", message.document.file_id
        else:
            # Only a consumed direct upload may restore its selected target.
            # Old invalid ForceReply replies cannot overwrite another invoice.
            if direct:
                receipt_intents.arm(
                    bot.id, message.from_user.id, kind="file",
                    request_id=order_id,
                )
            await bot.send_message(
                message.chat.id, tr(
                    "Нужен чек: фото или PDF/JPG/PNG/WEBP.",
                    "Please attach a photo or PDF/JPG/PNG/WEBP receipt.",
                ),
            )
            return
        try:
            order, reviewers = await submit_manual_receipt(
                factory, project_id=project_id, bot_id=bot.id,
                buyer_id=message.from_user.id, order_id=order_id,
                kind=kind, file_id=file_id,
            )
        except (ValueError, PermissionError) as exc:
            await bot.send_message(message.chat.id, str(exc))
            return
        receipt_intents.clear(
            bot.id, message.from_user.id, kind="file", request_id=order_id,
        )
        for reviewer in reviewers:
            try:
                async with recipient_language(factory, reviewer):
                    # Always show immutable evidence from the order, not an
                    # unrelated second reply to an already-submitted prompt.
                    if order.receipt_kind == "photo":
                        await bot.send_photo(reviewer, order.receipt_file_id)
                    else:
                        await bot.send_document(reviewer, order.receipt_file_id)
                    await send_payment_review(
                        bot, chat_id=reviewer,
                        rich_html=rich_file_review(
                            order, receipt_attached=True,
                        ),
                        legacy_text=(
                            "🧾 <b>" + escape(order.product_name) + "</b>\n"
                            + "💰 " + escape(_short_amount(order.amount)) + " "
                            + escape(order.currency_code) + "\n"
                            + tr("Проверьте поступление средств.",
                                 "Verify receipt of funds.")
                        ),
                        parse_mode="HTML",
                        reply_markup=_keyboard([[
                            _button(tr("✅ Подтвердить", "✅ Approve"),
                                    "approve:" + encode_uuid(order.id), "success"),
                            _button(tr("❌ Отклонить", "❌ Reject"),
                                    "reject:" + encode_uuid(order.id), "danger"),
                        ]]),
                    )
            except AiogramError:
                log.warning("Unable to deliver manual file receipt to reviewer %s", reviewer)
        await bot.send_message(
            message.chat.id,
            tr("📨 Чек зарегистрирован. Ожидайте проверки администратором.",
               "📨 Receipt recorded. Waiting for admin review."),
        )

    @router.message(ManualPriceFilter(editor))
    async def price_reply(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.text is None:
            return
        state = editor.get(message.from_user.id)
        if not state:
            return
        try:
            actor = await ensure_telegram_actor(
                factory, telegram_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
            )
            amount = await set_manual_price(
                factory, project_id=project_id, bot_id=bot.id,
                product_id=state["product"], actor_id=actor,
                currency=state["currency"], value=message.text,
            )
            editor.pop(message.from_user.id, None)
            await bot.send_message(
                message.chat.id, "✅ " + str(amount) + " " + state["currency"],
            )
            await show_manual_prices(
                bot, factory=factory, project_id=project_id,
                actor_id=actor, user_id=message.from_user.id,
                product_id=state["product"],
            )
        except (ValueError, PermissionError) as exc:
            await bot.send_message(message.chat.id, "⚠️ " + str(exc))

    return router
