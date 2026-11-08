"""Complete connected-bot files storefront and owner file editor."""
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
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.file_sales.manual_handlers import (
    show_file_payment_options,
)
from telegram_membership_saas.modules.file_sales.manual_service import (
    available_price_currencies,
    price_editor_options,
)
from telegram_membership_saas.modules.file_sales.models import FileAsset
from telegram_membership_saas.modules.file_sales.payment_controls import (
    list_file_payment_controls,
    set_all_file_payment_methods,
    toggle_file_payment_method,
)
from telegram_membership_saas.modules.file_sales.recovery import (
    recover_buyer_stars,
)
from telegram_membership_saas.modules.file_sales.rich_ui import (
    rich_file_card_html,
    send_file_card,
)
from telegram_membership_saas.modules.file_sales.service import (
    add_folder,
    add_product_asset,
    change_product_status,
    confirm_stars_payment,
    create_product,
    create_stars_order,
    list_folders,
    list_products,
    list_purchases_page,
    mark_file_delivery,
    precheckout_allowed,
    purchased_assets,
    update_draft_product,
    view_product,
)
from telegram_membership_saas.modules.owner_setup.service import (
    can_manage_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    decode_uuid,
    encode_uuid,
)

log = logging.getLogger(__name__)
ENTRY_SECONDS = 900


def _button(text: str, action: str, style: str = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data="fs:" + action, style=style,
    )


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _media_id(message: Message) -> tuple[str, str] | None:
    if message.has_protected_content:
        return None
    if message.document:
        return "document", message.document.file_id
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.audio:
        return "audio", message.audio.file_id
    return None


async def _deliver(
    bot: Bot, *, user_id: int, assets: tuple[FileAsset, ...],
    title: str, purchase_id: UUID,
) -> None:
    await bot.send_message(
        user_id,
        tr("✅ Ваша покупка: ", "✅ Your purchase: ") + escape(title),
        parse_mode="HTML",
    )
    for asset in assets:
        method = {
            "document": bot.send_document,
            "photo": bot.send_photo,
            "video": bot.send_video,
            "audio": bot.send_audio,
            "animation": bot.send_animation,
        }[asset.kind]
        await method(user_id, asset.file_id)
    await bot.send_message(
        user_id,
        tr("📦 Файлы доступны повторно через /myfiles.",
           "📦 You can download these files again using /myfiles."),
        reply_markup=_keyboard([[
            _button(tr("📥 Получить повторно", "📥 Download again"),
                    "get:" + encode_uuid(purchase_id)),
        ]]),
    )


class FileEditorFilter(BaseFilter):
    def __init__(self, pending: dict[int, dict]):
        self.pending = pending

    async def __call__(self, message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        # A pending admin editor must never swallow a Stars successful_payment
        # update (nor other service messages) before the payment router sees it.
        if getattr(message, "successful_payment", None) is not None:
            return False
        state = self.pending.get(message.from_user.id)
        if not state or state["expires"] <= monotonic():
            self.pending.pop(message.from_user.id, None)
            return False
        if message.text and message.text.startswith("/"):
            return False
        return True


def create_file_sales_router(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
) -> Router:
    router = Router(name="digital-file-sales")
    pending: dict[int, dict] = {}

    async def actor(user) -> UUID:
        return await ensure_telegram_actor(
            factory, telegram_id=user.id,
            username=user.username, first_name=user.first_name,
        )

    async def is_owner(user, bot: Bot) -> tuple[bool, UUID]:
        who = await actor(user)
        allowed = await can_manage_project(
            factory, actor_id=who, project_id=project_id,
        )
        return allowed, who

    async def home(bot: Bot, user_id: int, admin: bool) -> None:
        rows = [[_button(tr("🛍 Каталог файлов", "🛍 File catalog"), "shop")],
                [_button(tr("📥 Мои файлы", "📥 My files"), "mine")]]
        if admin:
            rows.append([_button(
                tr("⚙️ Управление продажами", "⚙️ Manage file sales"),
                "admin", "success",
            )])
            rows.append([InlineKeyboardButton(
                text=tr("🧾 Проверка оплаты файлов", "🧾 File payment reviews"),
                callback_data="fm:reviews",
                style="success",
            )])
        rows.append([InlineKeyboardButton(
            text=tr("⬅️ Главное меню", "⬅️ Main menu"),
            callback_data="own:dashboard" if admin else "checkoutnav:catalog",
        )])
        await bot.send_message(
            user_id,
            tr("📁 <b>Продажа файлов</b>\n"
               "Выберите удобный способ оплаты. После ручной проверки "
               "или оплаты звёздами Telegram файлы придут автоматически.",
               "📁 <b>Digital files</b>\n"
               "Choose your payment method. Files arrive after manual "
               "approval or confirmed Stars payment."),
            parse_mode="HTML", reply_markup=_keyboard(rows),
        )

    async def payment_method_settings(
        bot: Bot, user_id: int, actor_id: UUID,
    ) -> None:
        controls = await list_file_payment_controls(
            factory, project_id=project_id, bot_id=bot.id,
            actor_id=actor_id,
        )
        rows = []
        for method in controls:
            name = (tr("⭐ Автоматическая оплата звёздами (XTR)",
                       "⭐ Automatic Stars (XTR)")
                    if method.auto_stars else "💳 " + method.name[:36])
            rows.append([_button(
                ("🟢 " if method.enabled else "🔴 ") + name,
                "paytoggle:" + method.method_id.hex,
                "success" if method.enabled else "danger",
            )])
        # Exactly one bulk button; its label changes when any method is off.
        any_disabled = any(not method.enabled for method in controls)
        rows.append([_button(
            tr("✅ Включить все способы оплаты", "✅ Enable all payment methods")
            if any_disabled else
            tr("⏸️ Выключить все способы оплаты", "⏸️ Disable all payment methods"),
            "paybulk:" + ("on" if any_disabled else "off"),
            "success" if any_disabled else "danger",
        )])
        rows.append([_button(
            tr("⬅️ Управление продажами", "⬅️ File sales settings"), "admin",
        )])
        await bot.send_message(
            user_id,
            tr(
                "💳 <b>Способы оплаты файлов</b>\n"
                "🟢 Включён · 🔴 Выключен\n\n"
                "Эти настройки действуют только на продажи файлов "
                "в данном боте. Подписки и другие боты не затрагиваются.\n"
                "Ручной метод «Telegram Stars» и автоматическая оплата "
                "звёздами Telegram (XTR) — разные способы оплаты.\n"
                "Созданные заявки и оплаченные файлы сохраняются.",
                "💳 <b>File payment methods</b>\n"
                "🟢 Enabled · 🔴 Disabled\n\n"
                "Settings affect file sales in this bot only. "
                "Subscriptions and other bots are unchanged.\n"
                "A manual method named Telegram Stars and automatic "
                "Stars (XTR) are separate payment choices.\n"
                "Existing orders and paid files remain intact.",
            ),
            parse_mode="HTML", reply_markup=_keyboard(rows),
        )

    async def folders(bot: Bot, user_id: int, who: UUID | None) -> None:
        data = await list_folders(
            factory, project_id=project_id, bot_id=bot.id, actor_id=who,
        )
        rows = [[_button(
            ("📁 " if f.status == "active" else "📦 ") + f.name[:42],
            ("af:" if who else "folder:") + encode_uuid(f.id),
        )] for f in data]
        if who:
            rows.append([_button(tr("➕ Создать папку", "➕ New folder"),
                                 "newfolder", "success")])
            rows.append([_button(tr("⬅️ Управление", "⬅️ Manage"), "admin")])
        else:
            rows.append([_button(tr("⬅️ Магазин", "⬅️ Shop"), "home")])
        await bot.send_message(
            user_id, tr("📁 Папки с файлами", "📁 File folders"),
            reply_markup=_keyboard(rows),
        )

    async def show_products(bot: Bot, uid: int, folder: UUID,
                            who: UUID | None) -> None:
        products = await list_products(
            factory, project_id=project_id, bot_id=bot.id,
            folder_id=folder, actor_id=who,
        )
        rows = [[_button(
            ("🗃️ " if product.status != "published" else "📦 ")
            + product.name[:45],
            ("ap:" if who else "item:") + encode_uuid(product.id),
        )] for product in products]
        if who:
            rows.append([_button(tr("➕ Новый товар", "➕ New product"),
                                 "newproduct:" + encode_uuid(folder), "success")])
            rows.append([_button(tr("⬅️ Папки", "⬅️ Folders"), "adminfolders")])
        else:
            rows.append([_button(tr("⬅️ Папки", "⬅️ Folders"), "shop")])
        await bot.send_message(
            uid, tr("📦 Товары в папке", "📦 Folder products"),
            reply_markup=_keyboard(rows),
        )

    async def detail(bot: Bot, uid: int, item_id: UUID,
                     who: UUID | None) -> None:
        item, count = await view_product(
            factory, project_id=project_id, bot_id=bot.id,
            product_id=item_id, actor_id=who,
        )
        text = (
            "📦 <b>" + escape(item.name) + "</b>\n"
            + (escape(item.description) + "\n" if item.description else "")
            + (tr("⭐ Цена в звёздах Telegram: ", "⭐ Telegram Stars price: ")
               + str(item.stars_price) + " ⭐\n"
               if item.stars_price is not None else "")
            + tr("📎 Файлов: ", "📎 Files: ") + str(count)
        )
        code = encode_uuid(item.id)
        if who:
            prices = await price_editor_options(
                factory, project_id=project_id, bot_id=bot.id,
                product_id=item_id, actor_id=who,
            )
            if any(amount is not None for _, amount in prices):
                text += "\n" + tr("💱 Ручные цены: ", "💱 Manual prices: ")
                text += ", ".join(
                    escape(
                        (format(amount, "f").rstrip("0").rstrip(".")
                         if "." in format(amount, "f") else str(amount))
                        + " " + code
                    )
                    for code, amount in prices if amount is not None
                )
            text += "\n" + tr("Статус: ", "Status: ") + escape({
                "draft": tr("Черновик", "Draft"),
                "published": tr("В продаже", "Published"),
                "archived": tr("Снят с продажи", "Archived"),
            }.get(item.status, item.status))
            rows = []
            if item.status == "draft":
                rows.extend([
                    [_button(tr("🏷 Название", "🏷 Title"),
                             "editname:" + code),
                     _button(tr("⭐ Цена", "⭐ Price"),
                             "editprice:" + code)],
                    [_button(tr("📝 Описание", "📝 Description"),
                             "editdescription:" + code)],
                ])
                rows.append([_button(
                    tr("📎 Прикрепить файлы", "📎 Add files"),
                    "upload:" + code, "success",
                )])
                rows.append([InlineKeyboardButton(
                    text=tr("💱 Цены в валютах", "💱 Currency prices"),
                    callback_data="fm:prices:" + code,
                    style="success",
                )])
                if count:
                    rows.append([_button(
                        tr("✅ Опубликовать", "✅ Publish"), "publish:" + code,
                        "success",
                    )])
            else:
                rows.append([InlineKeyboardButton(
                    text=tr("💱 Цены в валютах", "💱 Currency prices"),
                    callback_data="fm:prices:" + code,
                    style="success",
                )])
                rows.append([_button(
                    tr("🗃️ Убрать с продажи", "🗃️ Archive")
                    if item.status == "published" else
                    tr("✅ Вернуть в продажу", "✅ Restore sale"),
                    ("archive:" if item.status == "published" else "publish:") + code,
                    "danger" if item.status == "published" else "success",
                )])
            rows.append([_button(
                tr("✨ Rich-предпросмотр", "✨ Rich preview"),
                "richpreview:" + code, "success",
            )])
            rows.append([_button(
                tr("⬅️ Товары папки", "⬅️ Folder products"),
                "af:" + encode_uuid(item.folder_id),
            )])
        else:
            rows = [
                [_button(tr("💳 Выбрать способ оплаты", "💳 Payment options"),
                         "pay:" + code, "success")],
                [_button(tr("⬅️ Товары", "⬅️ Products"),
                         "folder:" + encode_uuid(item.folder_id))],
            ]
        await bot.send_message(
            uid, text, parse_mode="HTML", reply_markup=_keyboard(rows),
        )

    async def rich_preview(bot: Bot, uid: int, item_id: UUID, actor: UUID) -> None:
        """Owner-only live API test without creating an invoice or affecting buyers."""
        item, count = await view_product(
            factory, project_id=project_id, bot_id=bot.id,
            product_id=item_id, actor_id=actor,
        )
        prices = await price_editor_options(
            factory, project_id=project_id, bot_id=bot.id,
            actor_id=actor, product_id=item_id,
        )
        price = next(
            (format(amount, "f") + " " + currency
             for currency, amount in prices if amount is not None),
            str(item.stars_price) + " XTR"
            if item.stars_price is not None else tr("Цена не задана", "Price not set"),
        )
        rich = rich_file_card_html(
            product_name=item.name, description=item.description,
            price=price, file_count=count,
        )
        legacy = (
            tr("✨ <b>Предпросмотр нового оформления</b>\n",
               "✨ <b>New design preview</b>\n")
            + "📦 <b>" + escape(item.name) + "</b>\n"
            + tr("💰 Цена: ", "💰 Price: ") + escape(price) + "\n"
            + tr("📎 Файлов: ", "📎 Files: ") + str(count)
        )
        markup = _keyboard([[_button(
            tr("⬅️ Карточка товара", "⬅️ Product card"),
            "ap:" + encode_uuid(item_id),
        )]])
        rich_sent = await send_file_card(
            bot, chat_id=uid, rich_html=rich,
            legacy_html=legacy, reply_markup=markup, preview=True,
        )
        if not rich_sent:
            await bot.send_message(
                uid, tr(
                    "ℹ️ Rich-сообщение не поддерживается этим API. "
                    "Показана обычная карточка; покупки работают как раньше.",
                    "ℹ️ This API does not support Rich Messages. "
                    "The classic card was shown; checkout is unchanged.",
                ),
            )

    async def my_files(bot: Bot, uid: int, page: int = 0) -> None:
        total, items = await list_purchases_page(
            factory, project_id=project_id, bot_id=bot.id,
            buyer_id=uid, page=page,
        )
        rows = [[_button(
            "📥 " + purchase.product_name[:45],
            "get:" + encode_uuid(purchase.id),
        )] for purchase in items]
        navigation = []
        if page:
            navigation.append(_button("◀️", "mine:" + str(page - 1)))
        if (page + 1) * 8 < total:
            navigation.append(_button("▶️", "mine:" + str(page + 1)))
        if navigation:
            rows.append(navigation)
        if page == 0:
            rows.append([_button(
                tr("🔄 Проверить оплату Stars", "🔄 Recover Stars payment"),
                "recover",
            )])
        rows.append([_button(tr("⬅️ Магазин", "⬅️ Shop"), "home")])
        await bot.send_message(
            uid, tr("📦 Купленных файлов: ", "📦 Purchased files: ")
            + str(total)
            + (tr("\\nСтраница: ", "\\nPage: ") + str(page + 1)
               if total > 8 else ""),
            reply_markup=_keyboard(rows),
        )

    async def deliver_paid(bot: Bot, uid: int, purchase_id: UUID) -> None:
        order, assets = await purchased_assets(
            factory, project_id=project_id, bot_id=bot.id,
            buyer_id=uid, purchase_id=purchase_id,
        )
        await _deliver(
            bot, user_id=uid, assets=assets,
            title=order.product_name, purchase_id=order.id,
        )
        await mark_file_delivery(
            factory, project_id=project_id, bot_id=bot.id,
            buyer_id=uid, purchase_id=order.id,
        )

    @router.message(Command("files", "file", "shop"))
    async def files_command(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        pending.pop(message.from_user.id, None)
        admin, _ = await is_owner(message.from_user, bot)
        await home(bot, message.from_user.id, admin)

    @router.message(Command("myfiles"))
    async def my_files_command(message: Message, bot: Bot) -> None:
        if message.from_user and message.chat.type == "private":
            pending.pop(message.from_user.id, None)
            await my_files(bot, message.from_user.id)

    @router.callback_query(F.data.startswith("fs:"))
    async def file_callback(query: CallbackQuery, bot: Bot) -> None:
        if (
            query.message is None or query.from_user is None
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer()
            return
        uid = query.from_user.id
        admin, who = await is_owner(query.from_user, bot)
        action = query.data[3:]
        try:
            if action == "home":
                pending.pop(uid, None)
                await home(bot, uid, admin)
            elif action in ("shop", "adminfolders"):
                await folders(bot, uid, who if action == "adminfolders" else None)
            elif action == "admin":
                if not admin:
                    raise PermissionError(tr(
                        "Нет доступа к управлению товарами",
                        "You cannot manage these products",
                    ))
                pending.pop(uid, None)
                await bot.send_message(
                    uid,
                    tr("⚙️ <b>Продажа файлов</b>\n"
                       "Создайте папку и товар, добавьте вложения, затем "
                       "опубликуйте его. Уже купленные файлы остаются у покупателя.",
                       "⚙️ <b>File sales</b>\n"
                       "Create a folder and product, attach files and publish. "
                       "Existing purchases remain available."),
                    parse_mode="HTML",
                    reply_markup=_keyboard([
                        [_button(tr("📁 Папки и товары", "📁 Folders and products"),
                                 "adminfolders")],
                        [_button(tr("💳 Способы оплаты файлов",
                                    "💳 File payment methods"),
                                 "paymethods", "success")],
                        [InlineKeyboardButton(
                            text=tr("🧾 Проверка оплаты файлов", "🧾 File payment reviews"),
                            callback_data="fm:reviews",
                            style="success",
                        )],
                        [_button(tr("⬅️ Магазин", "⬅️ Shop"), "home")],
                    ]),
                )
            elif action == "paymethods":
                if not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                await payment_method_settings(bot, uid, who)
            elif action.startswith("paytoggle:"):
                if not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                token = action.split(":", 1)[1]
                method_id = UUID(hex=token)
                await toggle_file_payment_method(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=who, method_id=method_id,
                )
                await payment_method_settings(bot, uid, who)
            elif action.startswith("paybulk:"):
                if not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                mode = action.split(":", 1)[1]
                if mode not in ("on", "off"):
                    raise ValueError(tr("Неизвестное действие", "Unknown action"))
                await set_all_file_payment_methods(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=who, enabled=mode == "on",
                )
                await payment_method_settings(bot, uid, who)
            elif action == "newfolder":
                if not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                pending[uid] = {"stage": "folder", "expires": monotonic() + ENTRY_SECONDS}
                await bot.send_message(
                    uid, tr("📁 Напишите название папки следующим сообщением. "
                            "/files — отмена.",
                            "📁 Send the new folder name as your next message. "
                            "/files cancels."),
                )
            elif action.startswith(("af:", "folder:")):
                owner_view, token = action.split(":", 1)
                if owner_view == "af" and not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                await show_products(
                    bot, uid, decode_uuid(token),
                    who if owner_view == "af" else None,
                )
            elif action.startswith("newproduct:"):
                if not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                folder = decode_uuid(action.split(":", 1)[1])
                # Verify folder belongs to this project before starting a draft.
                data = await list_folders(
                    factory, project_id=project_id, bot_id=bot.id, actor_id=who,
                )
                if not any(x.id == folder and x.status == "active" for x in data):
                    raise ValueError(tr("Папка не найдена", "Folder not found"))
                pending[uid] = {
                    "stage": "name", "folder": folder,
                    "expires": monotonic() + ENTRY_SECONDS,
                }
                await bot.send_message(
                    uid, tr("✏️ Название товара следующим сообщением. "
                            "/files — отмена.",
                            "✏️ Send the product title next. /files cancels."),
                )
            elif action.startswith("initcurrency:"):
                if not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                state = pending.get(uid)
                if not state or state.get("stage") != "currency":
                    raise ValueError(tr(
                        "Срок выбора валюты истёк",
                        "Currency selection has expired",
                    ))
                code = action.split(":", 1)[1]
                choices = await available_price_currencies(
                    factory, project_id=project_id, bot_id=bot.id, actor_id=who,
                )
                if code != "XTR" and code not in choices:
                    raise ValueError(tr("Валюта недоступна", "Currency unavailable"))
                state["currency"] = code
                state["stage"] = "price"
                state["expires"] = monotonic() + ENTRY_SECONDS
                await bot.send_message(
                    uid,
                    tr("💰 Укажите цену файла в ", "💰 Enter file price in ")
                    + code + tr(" следующим сообщением.",
                                " in your next message."),
                )
            elif action.startswith(("ap:", "item:")):
                admin_view, token = action.split(":", 1)
                if admin_view == "ap" and not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                pending.pop(uid, None)
                if admin_view == "item":
                    await show_file_payment_options(
                        bot, factory=factory, project_id=project_id,
                        buyer_id=uid, product_id=decode_uuid(token),
                    )
                else:
                    await detail(bot, uid, decode_uuid(token), who)
            elif action.startswith("pay:"):
                pending.pop(uid, None)
                await show_file_payment_options(
                    bot, factory=factory, project_id=project_id,
                    buyer_id=uid, product_id=decode_uuid(action.split(":")[1]),
                )
            elif action.startswith(("editname:", "editdescription:", "editprice:")):
                if not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                operation, token = action.split(":", 1)
                item_id = decode_uuid(token)
                product, _ = await view_product(
                    factory, project_id=project_id, bot_id=bot.id,
                    product_id=item_id, actor_id=who,
                )
                if product.status != "draft":
                    raise ValueError(tr(
                        "Можно изменить только черновик",
                        "Only drafts can be edited",
                    ))
                pending[uid] = {
                    "stage": operation, "product": item_id,
                    "expires": monotonic() + ENTRY_SECONDS,
                }
                await bot.send_message(uid, tr(
                    "✏️ Отправьте новое значение следующим сообщением. "
                    "/files — отмена.",
                    "✏️ Send the new value in the next message. "
                    "/files cancels.",
                ))
            elif action.startswith("upload:"):
                if not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                product = decode_uuid(action.split(":", 1)[1])
                await view_product(
                    factory, project_id=project_id, bot_id=bot.id,
                    product_id=product, actor_id=who,
                )
                pending[uid] = {
                    "stage": "upload", "product": product,
                    "expires": monotonic() + ENTRY_SECONDS,
                }
                await bot.send_message(
                    uid, tr("📎 Отправляйте файлы, фото, видео, GIF или аудио "
                            "отдельными сообщениями (до 20 вложений).",
                            "📎 Send files, photos, videos, GIFs or audio "
                            "as messages (up to 20 items)."),
                    reply_markup=_keyboard([[
                        _button(tr("✅ Готово", "✅ Done"),
                                "ap:" + encode_uuid(product), "success"),
                    ]]),
                )
            elif action.startswith(("publish:", "archive:")):
                if not admin:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                verb, token = action.split(":", 1)
                product = decode_uuid(token)
                await change_product_status(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=who, product_id=product,
                    status="published" if verb == "publish" else "archived",
                )
                await detail(bot, uid, product, who)
            elif action.startswith("richpreview:"):
                if not admin or who is None:
                    raise PermissionError(tr("Нет прав", "Access denied"))
                await rich_preview(
                    bot, uid, decode_uuid(action.split(":", 1)[1]), who,
                )
            elif action.startswith("buy:"):
                product = decode_uuid(action.split(":", 1)[1])
                order = await create_stars_order(
                    factory, project_id=project_id,
                    bot_id=bot.id, buyer_id=uid, product_id=product,
                )
                if order.paid:
                    await deliver_paid(bot, uid, order.id)
                else:
                    await bot.send_invoice(
                        chat_id=uid, title=order.product_name[:32],
                        description=tr(
                            "Цифровые файлы. Доступ после подтверждения Stars.",
                            "Digital files delivered after successful Stars payment.",
                        ),
                        payload=order.invoice_payload,
                        currency="XTR", prices=[
                            LabeledPrice(label=order.product_name[:32],
                                         amount=order.amount),
                        ], provider_token="",
                    )
            elif action == "mine" or action.startswith("mine:"):
                raw = action.split(":", 1)[1] if ":" in action else "0"
                if not raw.isascii() or not raw.isdecimal():
                    raise ValueError(tr("Неверная страница", "Invalid page"))
                await my_files(bot, uid, int(raw))
            elif action == "recover":
                # Independent evidence comes from Telegram's actual Stars ledger.
                # The buyer cannot forge the transaction id or receipt.
                try:
                    recovered = await recover_buyer_stars(
                        factory, bot=bot, project_id=project_id, buyer_id=uid,
                    )
                except Exception:
                    log.exception("Stars reconciliation failed for bot %s", bot.id)
                    await bot.send_message(
                        uid, tr(
                            "⚠️ Сверка оплаты временно недоступна. "
                            "Попробуйте позже или откройте /support.",
                            "⚠️ Payment verification is temporarily unavailable. "
                            "Retry later or use /support.",
                        ),
                    )
                    await query.answer()
                    return
                await bot.send_message(
                    uid,
                    (tr("✅ Восстановлено покупок: ", "✅ Purchases recovered: ")
                     + str(len(recovered))) if recovered else tr(
                         "ℹ️ Новых подтверждённых оплат в журнале Telegram "
                         "не найдено. Если Stars списаны — /support.",
                         "ℹ️ No new verified payments were found in "
                         "Telegram's ledger. If Stars were charged, use /support.",
                     ),
                )
                await my_files(bot, uid)
            elif action.startswith("get:"):
                await deliver_paid(
                    bot, uid, decode_uuid(action.split(":", 1)[1]),
                )
            else:
                raise ValueError(tr("Неизвестное действие", "Unknown action"))
            await query.answer()
        except (ValueError, PermissionError) as exc:
            await query.answer(str(exc)[:190], show_alert=True)
        except AiogramError:
            log.exception("File sales Telegram response failed")
            await query.answer(tr("Ошибка Telegram. Попробуйте снова.",
                                  "Telegram error. Try again."), show_alert=True)

    @router.message(FileEditorFilter(pending))
    async def editor_message(message: Message, bot: Bot) -> None:
        if message.from_user is None:
            return
        uid = message.from_user.id
        state = pending.get(uid)
        if state is None:
            return
        admin, who = await is_owner(message.from_user, bot)
        if not admin:
            pending.pop(uid, None)
            return
        try:
            stage = state["stage"]
            if stage == "folder":
                if not message.text:
                    raise ValueError(tr(
                        "Введите название папки текстом",
                        "Enter the folder name as text",
                    ))
                folder = await add_folder(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=who, name=message.text,
                )
                pending.pop(uid, None)
                await bot.send_message(uid, tr(
                    "✅ Папка создана.", "✅ Folder created.",
                ))
                await show_products(bot, uid, folder.id, who)
            elif stage == "name":
                if not message.text or not 1 <= len(message.text.strip()) <= 120:
                    raise ValueError(tr(
                        "Название товара — от 1 до 120 символов",
                        "Product name must contain 1–120 characters",
                    ))
                state["name"] = message.text.strip()
                state["stage"] = "currency"
                choices = await available_price_currencies(
                    factory, project_id=project_id, bot_id=bot.id, actor_id=who,
                )
                from telegram_membership_saas.modules.telegram_commerce.storefront import (
                    CURRENCY_ICONS,
                )
                rows = [[_button(
                    CURRENCY_ICONS.get(code, "💱") + " " + code,
                    "initcurrency:" + code, "success",
                )] for code in choices]
                rows.append([_button(
                    tr("⭐ Звёзды Telegram", "⭐ Telegram Stars"),
                    "initcurrency:XTR",
                )])
                await bot.send_message(
                    uid, tr("💰 Выберите валюту первой цены файла. "
                            "Дополнительные валюты добавите в карточке.",
                            "💰 Select the first file price currency. "
                            "You can add others in the product card."),
                    reply_markup=_keyboard(rows),
                )
            elif stage == "price":
                text = message.text or ""
                currency = state["currency"]
                if currency == "XTR":
                    if not text.isascii() or not text.isdecimal():
                        raise ValueError(tr(
                            "Укажите целое число Stars",
                            "Enter a whole number of Stars",
                        ))
                    stars = int(text)
                    if not 1 <= stars <= 250000:
                        raise ValueError(tr(
                            "Цена Stars: от 1 до 250000",
                            "Stars price must be between 1 and 250000",
                        ))
                    product = await create_product(
                        factory, project_id=project_id, bot_id=bot.id,
                        actor_id=who, folder_id=state["folder"],
                        name=state["name"], stars=stars,
                    )
                else:
                    # Amount and draft are validated/committed atomically:
                    # a typo never leaves an orphan product.
                    product = await create_product(
                        factory, project_id=project_id, bot_id=bot.id,
                        actor_id=who, folder_id=state["folder"],
                        name=state["name"], stars=None,
                        manual_currency=currency, manual_amount=text,
                    )
                pending.pop(uid, None)
                await detail(bot, uid, product.id, who)
            elif stage in ("editname", "editdescription", "editprice"):
                if not message.text:
                    raise ValueError(tr("Введите значение текстом", "Enter the value as text"))
                field = {
                    "editname": "name",
                    "editdescription": "description",
                    "editprice": "stars_price",
                }[stage]
                updated = await update_draft_product(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=who, product_id=state["product"],
                    field=field, value=message.text,
                )
                pending.pop(uid, None)
                await detail(bot, uid, updated.id, who)
            elif stage == "upload":
                media = _media_id(message)
                if media is None:
                    raise ValueError(tr(
                        "Нужен файл, фото, видео, GIF или аудио",
                        "Attach a file, photo, video, GIF or audio",
                    ))
                kind, file_id = media
                amount = await add_product_asset(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=who, product_id=state["product"],
                    kind=kind, file_id=file_id,
                )
                state["expires"] = monotonic() + ENTRY_SECONDS
                await bot.send_message(
                    uid, tr("✅ Вложений: ", "✅ Attachments: ") + str(amount),
                    reply_markup=_keyboard([[
                        _button(tr("✅ Готово", "✅ Done"),
                                "ap:" + encode_uuid(state["product"]), "success"),
                    ]]),
                )
        except (ValueError, PermissionError) as exc:
            await bot.send_message(uid, "⚠️ " + str(exc))

    @router.pre_checkout_query(F.invoice_payload.startswith("fs:"))
    async def file_pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
        if not query.invoice_payload.startswith("fs:"):
            return
        allowed = await precheckout_allowed(
            factory, project_id=project_id, bot_id=bot.id,
            buyer_id=query.from_user.id, invoice_payload=query.invoice_payload,
            amount=query.total_amount, currency=query.currency,
        )
        await bot.answer_pre_checkout_query(
            pre_checkout_query_id=query.id, ok=allowed,
            error_message=None if allowed else tr(
                "Товар недоступен или сумма изменилась.",
                "This product or amount is no longer available.",
            ),
        )

    @router.message(F.successful_payment.invoice_payload.startswith("fs:"))
    async def file_payment_success(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        payment = message.successful_payment
        if not payment.invoice_payload.startswith("fs:"):
            return
        try:
            purchase = await confirm_stars_payment(
                factory, project_id=project_id, bot_id=bot.id,
                buyer_id=message.from_user.id,
                invoice_payload=payment.invoice_payload,
                amount=payment.total_amount, currency=payment.currency,
                charge_id=payment.telegram_payment_charge_id,
            )
        except Exception:
            # Telegram charged the user, but DB confirmation is NOT durable.
            # Never tell them /myfiles is already unlocked. Recovery uses the
            # bot's authenticated Stars ledger, not buyer-supplied receipts.
            log.exception("CRITICAL: charged Stars payment not yet committed")
            try:
                await bot.send_message(
                    message.from_user.id,
                    tr(
                        "⚠️ Stars списаны, но бот не смог записать оплату. "
                        "После восстановления откройте /myfiles → "
                        "«Проверить оплату Stars» для сверки с Telegram. "
                        "Если не поможет — /support.",
                        "⚠️ Stars were charged, but payment recording failed. "
                        "Open /myfiles → Recover Stars payment to verify "
                        "against Telegram's ledger. Contact /support if needed.",
                    ),
                )
            except AiogramError:
                log.exception("Could not warn buyer about unrecorded Stars charge")
            return
        try:
            await deliver_paid(bot, message.from_user.id, purchase.id)
        except Exception:
            # Charge is now durably recorded. A direct /myfiles redownload
            # is safe even if this Telegram send failed midway.
            log.exception("Paid file delivery failed after durable commit")
            try:
                await bot.send_message(
                    message.from_user.id,
                    tr(
                        "✅ Оплата сохранена. Выдача прервалась — "
                        "откройте /myfiles для повторного получения.",
                        "✅ Payment was recorded. Delivery was interrupted; "
                        "open /myfiles to retry.",
                    ),
                )
            except AiogramError:
                log.exception("Could not warn buyer about delivery failure")

    return router
