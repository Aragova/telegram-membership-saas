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
from telegram_membership_saas.modules.file_sales.models import FileAsset, FileProduct
from telegram_membership_saas.modules.file_sales.service import (
    add_folder,
    add_product_asset,
    change_product_status,
    confirm_stars_payment,
    create_product,
    create_stars_order,
    list_folders,
    list_products,
    list_purchases,
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
            text=tr("⬅️ Главное меню", "⬅️ Main menu"),
            callback_data="own:dashboard" if admin else "checkoutnav:catalog",
        )])
        await bot.send_message(
            user_id,
            tr("📁 <b>Продажа файлов</b>\n"
               "Покупка — через Telegram Stars. После оплаты файлы "
               "приходят автоматически.",
               "📁 <b>Digital files</b>\n"
               "Pay with Telegram Stars; files are delivered automatically."),
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
            + product.name[:35] + " · " + str(product.stars_price) + " ⭐",
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
            + tr("💰 Цена: ", "💰 Price: ") + str(item.stars_price) + " ⭐\n"
            + tr("📎 Файлов: ", "📎 Files: ") + str(count)
        )
        code = encode_uuid(item.id)
        if who:
            text += "\n" + tr("Статус: ", "Status: ") + escape(item.status)
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
                if count:
                    rows.append([_button(
                        tr("✅ Опубликовать", "✅ Publish"), "publish:" + code,
                        "success",
                    )])
            else:
                rows.append([_button(
                    tr("🗃️ Убрать с продажи", "🗃️ Archive")
                    if item.status == "published" else
                    tr("✅ Вернуть в продажу", "✅ Restore sale"),
                    ("archive:" if item.status == "published" else "publish:") + code,
                    "danger" if item.status == "published" else "success",
                )])
            rows.append([_button(
                tr("⬅️ Товары папки", "⬅️ Folder products"),
                "af:" + encode_uuid(item.folder_id),
            )])
        else:
            rows = [
                [_button(tr("⭐ Купить", "⭐ Buy"), "buy:" + code, "success")],
                [_button(tr("⬅️ Товары", "⬅️ Products"),
                         "folder:" + encode_uuid(item.folder_id))],
            ]
        await bot.send_message(
            uid, text, parse_mode="HTML", reply_markup=_keyboard(rows),
        )

    async def my_files(bot: Bot, uid: int) -> None:
        items = await list_purchases(
            factory, project_id=project_id, bot_id=bot.id, buyer_id=uid,
        )
        rows = [[_button(
            "📥 " + purchase.product_name[:45],
            "get:" + encode_uuid(purchase.id),
        )] for purchase in items]
        rows.append([_button(tr("⬅️ Магазин", "⬅️ Shop"), "home")])
        await bot.send_message(
            uid, tr("📦 Купленных файлов: ", "📦 Purchased files: ")
            + str(len(items)),
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
                    raise PermissionError("Нет доступа к управлению товарами")
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
                        [_button(tr("⬅️ Магазин", "⬅️ Shop"), "home")],
                    ]),
                )
            elif action == "newfolder":
                if not admin:
                    raise PermissionError("Нет прав")
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
                    raise PermissionError("Нет прав")
                await show_products(
                    bot, uid, decode_uuid(token),
                    who if owner_view == "af" else None,
                )
            elif action.startswith("newproduct:"):
                if not admin:
                    raise PermissionError("Нет прав")
                folder = decode_uuid(action.split(":", 1)[1])
                # Verify folder belongs to this project before starting a draft.
                data = await list_folders(
                    factory, project_id=project_id, bot_id=bot.id, actor_id=who,
                )
                if not any(x.id == folder and x.status == "active" for x in data):
                    raise ValueError("Папка не найдена")
                pending[uid] = {
                    "stage": "name", "folder": folder,
                    "expires": monotonic() + ENTRY_SECONDS,
                }
                await bot.send_message(
                    uid, tr("✏️ Название товара следующим сообщением. "
                            "/files — отмена.",
                            "✏️ Send the product title next. /files cancels."),
                )
            elif action.startswith(("ap:", "item:")):
                admin_view, token = action.split(":", 1)
                if admin_view == "ap" and not admin:
                    raise PermissionError("Нет прав")
                pending.pop(uid, None)
                await detail(
                    bot, uid, decode_uuid(token),
                    who if admin_view == "ap" else None,
                )
            elif action.startswith(("editname:", "editdescription:", "editprice:")):
                if not admin:
                    raise PermissionError("Нет прав")
                operation, token = action.split(":", 1)
                item_id = decode_uuid(token)
                product, _ = await view_product(
                    factory, project_id=project_id, bot_id=bot.id,
                    product_id=item_id, actor_id=who,
                )
                if product.status != "draft":
                    raise ValueError("Можно изменить только черновик")
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
                    raise PermissionError("Нет прав")
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
                    raise PermissionError("Нет прав")
                verb, token = action.split(":", 1)
                product = decode_uuid(token)
                await change_product_status(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=who, product_id=product,
                    status="published" if verb == "publish" else "archived",
                )
                await detail(bot, uid, product, who)
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
            elif action == "mine":
                await my_files(bot, uid)
            elif action.startswith("get:"):
                await deliver_paid(
                    bot, uid, decode_uuid(action.split(":", 1)[1]),
                )
            else:
                raise ValueError("Неизвестное действие")
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
                    raise ValueError("Введите название папки текстом")
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
                    raise ValueError("Название товара — от 1 до 120 символов")
                state["name"] = message.text.strip()
                state["stage"] = "price"
                await bot.send_message(
                    uid, tr("⭐ Укажите цену в Stars целым числом (1–250000).",
                            "⭐ Enter the price in Stars (integer 1–250000)."),
                )
            elif stage == "price":
                text = message.text or ""
                if not text.isascii() or not text.isdecimal():
                    raise ValueError("Укажите целое число Stars")
                product = await create_product(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=who, folder_id=state["folder"],
                    name=state["name"], stars=int(text),
                )
                pending.pop(uid, None)
                await detail(bot, uid, product.id, who)
            elif stage in ("editname", "editdescription", "editprice"):
                if not message.text:
                    raise ValueError("Введите значение текстом")
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
                    raise ValueError("Нужен файл, фото, видео, GIF или аудио")
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

    @router.pre_checkout_query()
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

    @router.message(F.successful_payment)
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
            await deliver_paid(bot, message.from_user.id, purchase.id)
        except Exception:
            # Telegram already charged: do not mask a confirmed payment or
            # claim a refund. The buyer can retry /myfiles; alert operator.
            log.exception("Critical: digital Stars purchase delivery failed")
            await bot.send_message(
                message.from_user.id,
                tr("⚠️ Оплата получена, но выдача не завершилась. "
                   "Откройте /myfiles или обратитесь в /support.",
                   "⚠️ Payment received but delivery did not finish. "
                   "Use /myfiles or /support."),
            )

    return router
