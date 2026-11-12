"""Tenant-restricted digital products and Telegram Stars checkout.

A paid file order does not produce a channel Subscription or change the
existing manual-settlement Payment/PaymentRequest/Entitlement ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import (
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.bots.models import BotInstance, BotInstanceStatus
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayInvoice
from telegram_membership_saas.modules.file_sales.manual_models import (
    FileManualOrder,
    FileManualPrice,
)
from telegram_membership_saas.modules.file_sales.models import (
    FileAsset,
    FileFolder,
    FileProduct,
    FilePurchase,
)
from telegram_membership_saas.modules.file_sales.payment_control_models import (
    FileSaleMethodVisibility,
)
from telegram_membership_saas.modules.identity.models import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
    PaymentMethodStatus,
)
from telegram_membership_saas.modules.platform_saas_hold_errors import (
    translate_commercial_hold,
)
from telegram_membership_saas.modules.pricing import CurrencyAsset, CurrencyAssetStatus
from telegram_membership_saas.modules.projects.models import Project, ProjectStatus
from telegram_membership_saas.modules.telegram_admin.service import (
    _require_project_manager,
)
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext

ASSET_LIMIT = 20
FOLDER_LIMIT = 100
PRODUCT_LIMIT = 2000
PENDING_ORDER_LIMIT = 50


@dataclass(frozen=True, slots=True)
class PaidFileListing:
    id: UUID
    product_name: str


@dataclass(frozen=True, slots=True)
class DigitalOrder:
    id: UUID
    product_id: UUID
    product_name: str
    amount: int
    invoice_payload: str
    paid: bool


async def _owner(
    session: AsyncSession, *, project_id: UUID,
    actor_id: UUID, bot_id: int,
) -> None:
    await _require_project_manager(
        session, project_id=project_id, actor_user_id=actor_id,
    )
    bot = await session.scalar(
        select(BotInstance.id).where(
            BotInstance.project_id == project_id,
            BotInstance.telegram_bot_id == bot_id,
            BotInstance.status == BotInstanceStatus.REGISTERED.value,
        )
    )
    if bot is None:
        raise PermissionError(tr("Этот бот отключён", "This bot is disabled"))


async def _ready(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int | None = None,
) -> None:
    project = await session.scalar(select(Project.id).where(
        Project.id == project_id, Project.status == ProjectStatus.ACTIVE.value,
    ))
    bot = await session.scalar(select(BotInstance.id).where(
        BotInstance.project_id == project_id,
        BotInstance.telegram_bot_id == bot_id,
        BotInstance.status == BotInstanceStatus.REGISTERED.value,
    ))
    if project is None or bot is None:
        raise PermissionError(tr("Магазин недоступен", "This shop is unavailable"))
    if buyer_id is not None:
        banned = await session.scalar(
            select(ProjectCustomer.id)
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .where(
                ProjectCustomer.project_id == project_id,
                TelegramUser.telegram_user_id == buyer_id,
                ProjectCustomer.is_banned.is_(True),
            )
        )
        if banned is not None:
            raise PermissionError(tr(
                "Покупки в этом боте заблокированы",
                "Purchases in this bot are blocked",
            ))


async def list_folders(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID | None = None,
) -> tuple[FileFolder, ...]:
    if actor_id is None:
        scope = system_project_session_scope(factory, project_id)
    else:
        scope = tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id),
        )
    async with scope as session:
        if actor_id:
            await _owner(session, project_id=project_id, actor_id=actor_id,
                         bot_id=bot_id)
        else:
            await _ready(session, project_id=project_id, bot_id=bot_id)
        q = select(FileFolder).where(
            FileFolder.project_id == project_id,
            FileFolder.removed_at.is_(None),
        )
        if actor_id is None:
            q = q.where(
                FileFolder.status == "active",
                select(FileProduct.id).where(
                    FileProduct.project_id == project_id,
                    FileProduct.folder_id == FileFolder.id,
                    FileProduct.telegram_bot_id == bot_id,
                    FileProduct.status == "published",
                ).exists(),
            )
        return tuple((await session.scalars(
            q.order_by(FileFolder.created_at, FileFolder.id).limit(FOLDER_LIMIT)
        )).all())


async def add_folder(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID, name: str,
) -> FileFolder:
    name = name.strip()
    if not 1 <= len(name) <= 100:
        raise ValueError(tr(
            "Название папки: от 1 до 100 символов",
            "Folder name must contain 1–100 characters",
        ))
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        count = await session.scalar(select(func.count(FileFolder.id)).where(
            FileFolder.project_id == project_id,
            FileFolder.removed_at.is_(None),
        ))
        if count >= FOLDER_LIMIT:
            raise ValueError(tr("Достигнут лимит папок", "Folder limit reached"))
        folder = FileFolder(project_id=project_id, name=name, status="active")
        session.add(folder)
        await session.flush()
        return folder


async def delete_empty_folder(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID, folder_id: UUID,
) -> None:
    """Remove a folder with only unpaid invoices, retaining immutable history.

    Empty folders are physically deleted. Nonempty folders/products become
    invisible tombstones, so an old submitted receipt can still be reviewed
    and any later valid payment can still receive its originally sold assets.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        folder = await session.scalar(select(FileFolder).where(
            FileFolder.id == folder_id, FileFolder.project_id == project_id,
            FileFolder.removed_at.is_(None),
        ).with_for_update())
        if folder is None:
            raise ValueError(tr("Папка не найдена", "Folder not found"))
        products = (await session.scalars(select(FileProduct).where(
            FileProduct.folder_id == folder_id,
            FileProduct.project_id == project_id,
        ).with_for_update())).all()
        if any(item.telegram_bot_id != bot_id for item in products):
            raise ValueError(tr(
                "В папке есть товары другого бота. Удаление запрещено.",
                "This folder contains products from another bot.",
            ))
        for item in products:
            if await _has_paid_purchase(
                session, project_id=project_id, product_id=item.id,
            ):
                raise ValueError(tr(
                    "В папке есть оплаченные товары. Удаление запрещено; "
                    "оплаченные файлы должны оставаться доступными.",
                    "Folder contains paid purchases. Deletion is blocked "
                    "so existing buyers can redownload files.",
                ))
        if not products:
            await session.delete(folder)
            return
        now = datetime.now(UTC)
        for item in products:
            item.status = "archived"
            item.removed_at = now
        folder.status = "archived"
        folder.removed_at = now
        await session.flush()

async def create_product(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID, folder_id: UUID,
    name: str, stars: int | None, description: str = "",
    manual_currency: str | None = None, manual_amount: str | None = None,
) -> FileProduct:
    name = name.strip()
    if not 1 <= len(name) <= 120:
        raise ValueError(tr(
            "Название товара: от 1 до 120 символов",
            "Product name must contain 1–120 characters",
        ))
    if stars is not None and not 1 <= stars <= 250000:
        raise ValueError(tr(
            "Цена должна составлять от 1 до 250000 Stars",
            "Stars price must be between 1 and 250000",
        ))
    if len(description) > 1024:
        raise ValueError(tr(
            "Описание: не более 1024 символов",
            "Description must be at most 1024 characters",
        ))
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        # Folder deletion locks this same row before snapshotting products.
        # Lock and recheck here to prevent a new draft escaping tombstoning
        # during a concurrent delete operation.
        folder = await session.scalar(select(FileFolder.id).where(
            FileFolder.id == folder_id,
            FileFolder.project_id == project_id,
            FileFolder.status == "active",
            FileFolder.removed_at.is_(None),
        ).with_for_update())
        if folder is None:
            raise ValueError(tr("Папка недоступна", "Folder unavailable"))
        total = await session.scalar(select(func.count(FileProduct.id)).where(
            FileProduct.project_id == project_id,
            FileProduct.removed_at.is_(None),
        ))
        if total >= PRODUCT_LIMIT:
            raise ValueError(tr("Достигнут лимит товаров", "Product limit reached"))
        amount = None
        if manual_currency is not None:
            if manual_amount is None:
                raise ValueError(tr(
                    "Укажите цену в выбранной валюте",
                    "Enter a price in the selected currency",
                ))
            asset = await session.scalar(select(CurrencyAsset).where(
                CurrencyAsset.code == manual_currency,
                CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
            ))
            if asset is None:
                raise ValueError(tr("Валюта не активна", "Currency is inactive"))
            active_method = await session.scalar(
                select(PaymentMethod.id)
                .join(PaymentMethodCurrency, and_(
                    PaymentMethodCurrency.project_id == PaymentMethod.project_id,
                    PaymentMethodCurrency.payment_method_id == PaymentMethod.id,
                ))
                .where(
                    PaymentMethod.project_id == project_id,
                    PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
                    PaymentMethodCurrency.currency_code == manual_currency,
                ).limit(1)
            )
            if active_method is None:
                raise ValueError(tr(
                    "Нет активного способа оплаты для валюты",
                    "No active payment method for this currency",
                ))
            try:
                amount = Decimal(manual_amount.strip().replace(",", "."))
            except InvalidOperation as exc:
                raise ValueError(tr("Неверная сумма файла", "Invalid file price")) from exc
            if not amount.is_finite() or amount <= 0 or amount >= Decimal("1000000000000"):
                raise ValueError(tr(
                    "Укажите положительную цену файла",
                    "Enter a positive file price",
                ))
            with localcontext() as context:
                context.prec = 50
                if amount != amount.quantize(Decimal(1).scaleb(-asset.precision)):
                    raise ValueError(tr(
                        "Слишком много знаков после запятой",
                        "Too many decimal places",
                    ))
        elif stars is None:
            raise ValueError(tr(
                "Нужна цена Stars или ручная цена",
                "Set a Stars price or a manual price",
            ))
        product = FileProduct(
            project_id=project_id, folder_id=folder_id,
            telegram_bot_id=bot_id, name=name, stars_price=stars,
            description=description, status="draft",
        )
        session.add(product)
        await session.flush()
        if amount is not None:
            session.add(FileManualPrice(
                project_id=project_id, product_id=product.id,
                currency_code=manual_currency, amount=amount,
            ))
            await session.flush()
        return product


async def _product(
    session: AsyncSession, project_id: UUID, bot_id: int, product_id: UUID,
    *, lock: bool = False,
) -> FileProduct:
    q = select(FileProduct).where(
        FileProduct.id == product_id,
        FileProduct.project_id == project_id,
        FileProduct.telegram_bot_id == bot_id,
    )
    product = await session.scalar(q.with_for_update() if lock else q)
    if product is None:
        raise ValueError(tr("Товар не найден в этом боте", "Product not found in this bot"))
    return product


async def update_draft_product(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
    product_id: UUID, field: str, value: str,
) -> FileProduct:
    if field not in ("name", "description", "stars_price"):
        raise ValueError(tr("Недоступное поле товара", "This product field cannot be edited"))
    if field == "name":
        value = value.strip()
        if not 1 <= len(value) <= 120:
            raise ValueError(tr(
                "Название товара: от 1 до 120 символов",
                "Product name must contain 1–120 characters",
            ))
    elif field == "description":
        value = value.strip()
        if len(value) > 1024:
            raise ValueError(tr(
                "Описание: не более 1024 символов",
                "Description must be at most 1024 characters",
            ))
    else:
        if not value.isascii() or not value.isdecimal():
            raise ValueError(tr(
                "Цена должна быть целым числом Stars",
                "Stars price must be a whole number",
            ))
        if not 1 <= int(value) <= 250000:
            raise ValueError(tr(
                "Цена должна составлять от 1 до 250000 Stars",
                "Stars price must be between 1 and 250000",
            ))
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id,
                     actor_id=actor_id, bot_id=bot_id)
        product = await _product(
            session, project_id, bot_id, product_id, lock=True,
        )
        if product.status != "draft":
            raise ValueError(tr("Редактировать можно только черновик", "Only drafts can be edited"))
        setattr(product, field, int(value) if field == "stars_price" else value)
        await session.flush()
        return product


async def add_product_asset(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
    product_id: UUID, kind: str, file_id: str,
) -> int:
    if kind not in ("document", "photo", "video", "audio", "animation"):
        raise ValueError(tr("Неподдерживаемый тип файла", "Unsupported file type"))
    if not 1 <= len(file_id) <= 512:
        raise ValueError(tr("Недопустимый Telegram файл", "Invalid Telegram file"))
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        product = await _product(
            session, project_id, bot_id, product_id, lock=True,
        )
        if product.status != "draft":
            raise ValueError(tr(
                "Добавление файлов возможно только в черновик",
                "Files can only be added to drafts",
            ))
        count = await session.scalar(select(func.count(FileAsset.id)).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
        )) or 0
        if count >= ASSET_LIMIT:
            raise ValueError(tr(
                "Не более 20 вложений в одном товаре",
                "Maximum 20 attachments per product",
            ))
        session.add(FileAsset(
            project_id=project_id, product_id=product_id,
            kind=kind, file_id=file_id, sort_order=count,
        ))
        return count + 1


async def _has_orders(
    session: AsyncSession, *, project_id: UUID, product_id: UUID,
) -> bool:
    """All orders, including canceled/rejected/pending, protect their product."""
    for model in (FilePurchase, FileManualOrder, CryptoPayInvoice):
        exists = await session.scalar(select(model.id).where(
            model.project_id == project_id,
            model.product_id == product_id,
        ).limit(1))
        if exists is not None:
            return True
    return False


async def _has_paid_purchase(
    session: AsyncSession, *, project_id: UUID, product_id: UUID,
) -> bool:
    """Confirmed Stars charges or approved manual file orders block deletion."""
    stars = await session.scalar(select(FilePurchase.id).where(
        FilePurchase.project_id == project_id,
        FilePurchase.product_id == product_id,
        FilePurchase.status == "paid",
    ).limit(1))
    if stars is not None:
        return True
    manual = await session.scalar(select(FileManualOrder.id).where(
        FileManualOrder.project_id == project_id,
        FileManualOrder.product_id == product_id,
        FileManualOrder.status == "approved",
    ).limit(1))
    if manual is not None:
        return True
    crypto = await session.scalar(select(CryptoPayInvoice.id).where(
        CryptoPayInvoice.project_id == project_id,
        CryptoPayInvoice.product_id == product_id,
        CryptoPayInvoice.kind == "file",
        CryptoPayInvoice.status == "paid",
    ).limit(1))
    return crypto is not None


async def delete_unsold_product(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID, product_id: UUID,
) -> UUID:
    """Unpaid orders do not block removal; immutable invoices remain intact.

    Physical deletion is allowed only without any order. Existing unpaid
    invoice rows require a hidden tombstone because the PostgreSQL schema
    deliberately forbids deleting even rejected/cancelled order snapshots.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        item = await _product(session, project_id, bot_id, product_id, lock=True)
        if item.removed_at is not None:
            raise ValueError(tr("Товар уже удалён", "Product already removed"))
        if await _has_paid_purchase(
            session, project_id=project_id, product_id=product_id,
        ):
            raise ValueError(tr(
                "У товара есть реальная покупка. Удалять его нельзя: "
                "покупатель должен сохранять доступ к файлу.",
                "This file has a confirmed purchase. Removal is blocked "
                "to preserve the buyer's file access.",
            ))
        folder_id = item.folder_id
        if await _has_orders(session, project_id=project_id, product_id=product_id):
            item.status = "archived"
            item.removed_at = datetime.now(UTC)
            await session.flush()
            return folder_id
        await session.execute(delete(FileAsset).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
        ))
        await session.execute(delete(FileManualPrice).where(
            FileManualPrice.project_id == project_id,
            FileManualPrice.product_id == product_id,
        ))
        await session.delete(item)
        return folder_id

async def list_product_assets(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID, product_id: UUID,
) -> tuple[FileProduct, tuple[FileAsset, ...]]:
    """Owner-only inventory with file IDs kept server-side, never in a card."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        item = await _product(session, project_id, bot_id, product_id)
        assets = tuple((await session.scalars(select(FileAsset).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
        ).order_by(FileAsset.sort_order, FileAsset.id).limit(ASSET_LIMIT))).all())
        return item, assets


async def delete_draft_asset(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
    product_id: UUID, asset_id: UUID,
) -> int:
    """Delete a single draft attachment, maintaining contiguous sort_order.

    Published/archived products might already have buyers, so their media is
    immutable. Serializes against adding new draft assets via product lock.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        item = await _product(session, project_id, bot_id, product_id, lock=True)
        if item.status != "draft" or await _has_orders(
            session, project_id=project_id, product_id=product_id,
        ):
            raise ValueError(tr(
                "Изменять вложения можно только у черновика без покупок",
                "Only draft products with no orders can change attachments",
            ))
        asset = await session.scalar(select(FileAsset).where(
            FileAsset.id == asset_id,
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
        ))
        if asset is None:
            raise ValueError(tr("Файл уже удалён", "File already deleted"))
        old_index = asset.sort_order
        await session.delete(asset)
        await session.flush()
        # Position uniqueness is enforced per product. Advance in ascending
        # order so each individual update always moves into a vacated slot.
        remaining = (await session.scalars(select(FileAsset).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
            FileAsset.sort_order > old_index,
        ).order_by(FileAsset.sort_order).with_for_update())).all()
        for other in remaining:
            other.sort_order -= 1
            await session.flush()
        count = await session.scalar(select(func.count(FileAsset.id)).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
        )) or 0
        return count


async def change_product_status(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
    product_id: UUID, status: str,
) -> None:
    if status not in ("published", "archived"):
        raise ValueError(tr("Недопустимый статус товара", "Invalid product status"))
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        item = await _product(session, project_id, bot_id, product_id, lock=True)
        if item.removed_at is not None:
            raise ValueError(tr("Товар удалён", "Product was removed"))
        if status == "published":
            assets = await session.scalar(select(func.count(FileAsset.id)).where(
                FileAsset.project_id == project_id,
                FileAsset.product_id == product_id,
            )) or 0
            if not assets:
                raise ValueError(tr("Сначала прикрепите файлы", "Attach files first"))
            folder = await session.scalar(select(FileFolder.id).where(
                FileFolder.project_id == project_id,
                FileFolder.id == item.folder_id,
                FileFolder.status == "active",
                FileFolder.removed_at.is_(None),
            ))
            if folder is None:
                raise ValueError(tr("Папка архивирована", "Folder is archived"))
            if item.stars_price is None:
                manual_prices = await session.scalar(select(func.count(
                    FileManualPrice.product_id,
                )).where(
                    FileManualPrice.project_id == project_id,
                    FileManualPrice.product_id == product_id,
                )) or 0
                if not manual_prices:
                    raise ValueError(tr(
                        "Укажите хотя бы одну цену перед публикацией",
                        "Set at least one price before publishing",
                    ))
        item.status = status


async def list_products(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, folder_id: UUID,
    actor_id: UUID | None = None,
) -> tuple[FileProduct, ...]:
    scope = (
        tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id),
        ) if actor_id else system_project_session_scope(factory, project_id)
    )
    async with scope as session:
        if actor_id:
            await _owner(session, project_id=project_id, actor_id=actor_id,
                         bot_id=bot_id)
        else:
            await _ready(session, project_id=project_id, bot_id=bot_id)
        q = select(FileProduct).where(
            FileProduct.project_id == project_id,
            FileProduct.telegram_bot_id == bot_id,
            FileProduct.folder_id == folder_id,
            FileProduct.removed_at.is_(None),
        )
        if not actor_id:
            folder = await session.scalar(select(FileFolder.id).where(
                FileFolder.id == folder_id,
                FileFolder.project_id == project_id,
                FileFolder.status == "active",
                FileFolder.removed_at.is_(None),
            ))
            if folder is None:
                return ()
            q = q.where(FileProduct.status == "published")
        return tuple((await session.scalars(
            q.order_by(FileProduct.created_at, FileProduct.id).limit(100)
        )).all())


async def view_product(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, product_id: UUID,
    actor_id: UUID | None = None,
) -> tuple[FileProduct, int]:
    scope = (
        tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id),
        ) if actor_id else system_project_session_scope(factory, project_id)
    )
    async with scope as session:
        if actor_id:
            await _owner(session, project_id=project_id, actor_id=actor_id,
                         bot_id=bot_id)
        else:
            await _ready(session, project_id=project_id, bot_id=bot_id)
        product = await _product(session, project_id, bot_id, product_id)
        if product.removed_at is not None:
            raise ValueError(tr("Товар удалён из каталога", "Product was removed"))
        if actor_id is None and product.status != "published":
            raise ValueError(tr("Товар не продаётся", "Product is not for sale"))
        count = await session.scalar(select(func.count(FileAsset.id)).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
        )) or 0
        return product, count


async def _load_paid(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int, product_id: UUID,
) -> FilePurchase | None:
    return await session.scalar(
        select(FilePurchase).where(
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.product_id == product_id,
            FilePurchase.status == "paid",
        ).order_by(FilePurchase.paid_at.desc()).limit(1)
    )


async def paid_file_purchase_id(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, product_id: UUID,
) -> UUID | None:
    """Return any rail's already-paid purchase; files are bought only once.

    A confirmed crypto invoice is identical ownership evidence to a paid
    Stars order or approved bank transfer. Purchase history remains durable.
    """
    async with system_project_session_scope(factory, project_id) as session:
        stars = await session.scalar(select(FilePurchase.id).where(
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.product_id == product_id,
            FilePurchase.status == "paid",
        ).limit(1))
        if stars is not None:
            return stars
        manual = await session.scalar(select(FileManualOrder.id).where(
            FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.telegram_user_id == buyer_id,
            FileManualOrder.product_id == product_id,
            FileManualOrder.status == "approved",
        ).limit(1))
        if manual is not None:
            return manual
        return await session.scalar(select(CryptoPayInvoice.id).where(
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.telegram_bot_id == bot_id,
            CryptoPayInvoice.telegram_user_id == buyer_id,
            CryptoPayInvoice.product_id == product_id,
            CryptoPayInvoice.kind == "file",
            CryptoPayInvoice.status == "paid",
        ).limit(1))


async def _has_authorized_file_stars(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int, product_id: UUID,
) -> bool:
    """Telegram Stars may be charged after precheckout says yes.

    Until a verified Stars transaction resolves this pending authorization,
    another rail must not issue a payable quote for the same one-time file.
    This is durable across cold restarts and not guessed from a local timer.
    """
    authorized = await session.scalar(select(FilePurchase.id).where(
        FilePurchase.project_id == project_id,
        FilePurchase.telegram_bot_id == bot_id,
        FilePurchase.telegram_user_id == buyer_id,
        FilePurchase.product_id == product_id,
        FilePurchase.status == "pending",
        FilePurchase.stars_authorized_at.is_not(None),
    ).limit(1))
    return authorized is not None


async def _has_unsettled_crypto_file_invoice(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int, product_id: UUID,
) -> bool:
    """A one-time file cannot get a second charge while Crypto Pay is payable.

    Only Crypto Pay itself may end 'issuing' or 'issued', after provider
    verification. An old button or local timer never makes its link safe.
    """
    existing = await session.scalar(select(CryptoPayInvoice.id).where(
        CryptoPayInvoice.project_id == project_id,
        CryptoPayInvoice.telegram_bot_id == bot_id,
        CryptoPayInvoice.telegram_user_id == buyer_id,
        CryptoPayInvoice.product_id == product_id,
        CryptoPayInvoice.kind == "file",
        CryptoPayInvoice.status.in_(("issuing", "issued")),
    ).limit(1))
    return existing is not None


@translate_commercial_hold
async def create_stars_order(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, product_id: UUID,
) -> DigitalOrder:
    async with system_project_session_scope(factory, project_id) as session:
        # Serialize new Stars orders with crypto invoice reservation.
        await checkout_retirement_lock(session, project_id)
        await _ready(
            session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id,
        )
        item = await _product(session, project_id, bot_id, product_id, lock=True)
        if item.status != "published":
            raise ValueError(tr("Товар больше не продаётся", "This product is no longer for sale"))
        if item.stars_price is None:
            raise ValueError(tr(
                "Этот товар не продаётся за Stars",
                "This product is not sold for Stars",
            ))
        folder = await session.scalar(select(FileFolder.id).where(
            FileFolder.project_id == project_id, FileFolder.id == item.folder_id,
            FileFolder.status == "active",
            FileFolder.removed_at.is_(None),
        ))
        if folder is None:
            raise ValueError(tr("Товар больше не продаётся", "This product is no longer for sale"))
        count = await session.scalar(select(func.count(FileAsset.id)).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
        )) or 0
        if count == 0:
            raise ValueError(tr("Товар не содержит файлов", "This product contains no files"))
        paid = await _load_paid(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
        )
        if paid:
            return DigitalOrder(
                id=paid.id, product_id=product_id,
                product_name=paid.product_name,
                amount=paid.stars_amount,
                invoice_payload=paid.invoice_payload, paid=True,
            )
        manual_paid = await session.scalar(select(FileManualOrder).where(
            FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.telegram_user_id == buyer_id,
            FileManualOrder.product_id == product_id,
            FileManualOrder.status == "approved",
        ).limit(1))
        if manual_paid:
            return DigitalOrder(
                id=manual_paid.id, product_id=product_id,
                product_name=manual_paid.product_name,
                amount=item.stars_price, invoice_payload="", paid=True,
            )
        crypto_paid = await session.scalar(select(CryptoPayInvoice).where(
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.telegram_bot_id == bot_id,
            CryptoPayInvoice.telegram_user_id == buyer_id,
            CryptoPayInvoice.product_id == product_id,
            CryptoPayInvoice.kind == "file",
            CryptoPayInvoice.status == "paid",
        ).limit(1))
        if crypto_paid is not None:
            return DigitalOrder(
                id=crypto_paid.id, product_id=product_id,
                product_name=crypto_paid.name_snapshot,
                amount=item.stars_price, invoice_payload="", paid=True,
            )
        if await _has_unsettled_crypto_file_invoice(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
        ):
            raise ValueError(tr(
                "Для файла уже открыт счёт Crypto Pay. Дождитесь оплаты или "
                "подтверждённого истечения счёта, прежде чем платить Stars.",
                "This file has an outstanding Crypto Pay invoice. Wait for "
                "payment or confirmed expiry before paying with Stars.",
            ))
        # A disabled storefront method cannot create a new Stars invoice.
        # Already-paid orders above remain downloadable.
        disabled = await session.scalar(select(FileSaleMethodVisibility.enabled).where(
            FileSaleMethodVisibility.project_id == project_id,
            FileSaleMethodVisibility.telegram_bot_id == bot_id,
            FileSaleMethodVisibility.method_id == UUID(int=0),
        ))
        if disabled is False:
            raise ValueError(tr(
                "Автоматическая оплата Stars для файлов отключена",
                "Automatic Stars payment for files is disabled",
            ))
        pending = await session.scalar(select(FilePurchase).where(
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.product_id == product_id,
            FilePurchase.status == "pending",
        ).order_by(FilePurchase.created_at.desc()).limit(1))
        if pending and pending.stars_amount == item.stars_price:
            return DigitalOrder(
                id=pending.id, product_id=product_id,
                product_name=pending.product_name, amount=pending.stars_amount,
                invoice_payload=pending.invoice_payload, paid=False,
            )
        total = await session.scalar(select(func.count(FilePurchase.id)).where(
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.status == "pending",
        )) or 0
        if total >= PENDING_ORDER_LIMIT:
            raise ValueError(tr(
                "Слишком много незавершённых покупок",
                "Too many unfinished purchases",
            ))
        invoice_payload = "fs:" + uuid4().hex
        record = FilePurchase(
            project_id=project_id, product_id=product_id,
            telegram_bot_id=bot_id, telegram_user_id=buyer_id,
            stars_amount=item.stars_price, product_name=item.name,
            invoice_payload=invoice_payload, status="pending",
        )
        session.add(record)
        await session.flush()
        return DigitalOrder(
            id=record.id, product_id=product_id,
            product_name=record.product_name, amount=record.stars_amount,
            invoice_payload=record.invoice_payload, paid=False,
        )


async def precheckout_allowed(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int,
    invoice_payload: str, amount: int, currency: str,
) -> bool:
    if not invoice_payload.startswith("fs:") or currency != "XTR":
        return False
    async with system_project_session_scope(factory, project_id) as session:
        try:
            await checkout_retirement_lock(session, project_id)
            await _ready(
                session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id,
            )
            purchase = await session.scalar(select(FilePurchase).where(
                FilePurchase.project_id == project_id,
                FilePurchase.telegram_bot_id == bot_id,
                FilePurchase.telegram_user_id == buyer_id,
                FilePurchase.invoice_payload == invoice_payload,
                FilePurchase.status == "pending",
                FilePurchase.stars_amount == amount,
            ))
            if purchase is None:
                return False
            # A pending Stars invoice must not charge again after an
            # authenticated Crypto Pay purchase settled for this file.
            owned_crypto = await session.scalar(select(CryptoPayInvoice.id).where(
                CryptoPayInvoice.project_id == project_id,
                CryptoPayInvoice.telegram_bot_id == bot_id,
                CryptoPayInvoice.telegram_user_id == buyer_id,
                CryptoPayInvoice.product_id == purchase.product_id,
                CryptoPayInvoice.kind == "file",
                CryptoPayInvoice.status == "paid",
            ).limit(1))
            if owned_crypto is not None or await _has_unsettled_crypto_file_invoice(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, product_id=purchase.product_id,
            ):
                return False
            disabled = await session.scalar(select(FileSaleMethodVisibility.enabled).where(
                FileSaleMethodVisibility.project_id == project_id,
                FileSaleMethodVisibility.telegram_bot_id == bot_id,
                FileSaleMethodVisibility.method_id == UUID(int=0),
            ))
            if disabled is False:
                return False
            item = await _product(session, project_id, bot_id, purchase.product_id)
            allowed = item.status == "published" and (
                await session.scalar(select(func.count(FileAsset.id)).where(
                    FileAsset.project_id == project_id,
                    FileAsset.product_id == purchase.product_id,
                )) or 0
            ) > 0
            if allowed:
                # Commit authorization BEFORE answering Telegram ok=True.
                # A later Crypto Pay reservation sees this even if the Stars
                # successful_payment update is delayed, lost or reordered.
                purchase.stars_authorized_at = datetime.now(UTC)
                await session.flush()
            return allowed
        except (PermissionError, ValueError):
            return False


async def confirm_stars_payment(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int,
    invoice_payload: str, amount: int, currency: str,
    charge_id: str,
    payment_at: datetime | None = None,
) -> FilePurchase:
    if currency != "XTR" or not charge_id or not invoice_payload.startswith("fs:"):
        raise ValueError(tr(
            "Это не платёж за цифровой товар",
            "This is not a digital file payment",
        ))
    async with system_project_session_scope(factory, project_id) as session:
        purchase = await session.scalar(select(FilePurchase).where(
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.invoice_payload == invoice_payload,
        ).with_for_update())
        if purchase is None or amount != purchase.stars_amount:
            raise PermissionError(tr(
                "Платёж не соответствует заказу этого бота",
                "Payment does not match this bot's order",
            ))
        if purchase.status == "paid":
            if purchase.telegram_charge_id != charge_id:
                raise PermissionError(tr(
                    "Заказ уже оплачен другим платежом",
                    "This order was already paid by another payment",
                ))
            return purchase
        if purchase.status != "pending":
            raise ValueError(tr("Неверное состояние заказа", "Invalid order status"))
        # Only the authenticated Telegram successful_payment message or
        # bot-owned StarTransaction ledger supplies payment_at. Preserve that
        # actual charge time for the 0055 non-retroactive billing cutoff,
        # historical owner identity and reward rate. Recovery time is NOT
        # the purchase time.
        if payment_at is not None and payment_at.tzinfo is None:
            raise ValueError("Telegram Stars payment time must include timezone")
        now = datetime.now(UTC)
        event_time = payment_at.astimezone(UTC) if payment_at else now
        if event_time > now + timedelta(minutes=5):
            raise ValueError("Future-dated Telegram Stars payment")
        purchase.telegram_charge_id = charge_id
        purchase.status = "paid"
        purchase.paid_at = event_time
        await session.flush()
        return purchase


async def list_purchases(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int,
) -> tuple[FilePurchase, ...]:
    async with system_project_session_scope(factory, project_id) as session:
        return tuple((await session.scalars(
            select(FilePurchase).where(
                FilePurchase.project_id == project_id,
                FilePurchase.telegram_bot_id == bot_id,
                FilePurchase.telegram_user_id == buyer_id,
                FilePurchase.status == "paid",
            ).order_by(FilePurchase.paid_at.desc()).limit(30)
        )).all())


async def list_purchases_page(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, page: int = 0,
) -> tuple[int, tuple[PaidFileListing, ...]]:
    """Unified paid history, including manual and Stars purchases (archived too)."""
    if not 0 <= page <= 10000:
        raise ValueError(tr("Неверная страница", "Invalid page"))
    async with system_project_session_scope(factory, project_id) as session:
        combined = union_all(
            select(
                FilePurchase.id.label("id"),
                FilePurchase.product_name.label("product_name"),
                FilePurchase.paid_at.label("paid_at"),
            ).where(
                FilePurchase.project_id == project_id,
                FilePurchase.telegram_bot_id == bot_id,
                FilePurchase.telegram_user_id == buyer_id,
                FilePurchase.status == "paid",
            ),
            select(
                FileManualOrder.id.label("id"),
                FileManualOrder.product_name.label("product_name"),
                FileManualOrder.paid_at.label("paid_at"),
            ).where(
                FileManualOrder.project_id == project_id,
                FileManualOrder.telegram_bot_id == bot_id,
                FileManualOrder.telegram_user_id == buyer_id,
                FileManualOrder.status == "approved",
            ),
            select(
                CryptoPayInvoice.id.label("id"),
                CryptoPayInvoice.name_snapshot.label("product_name"),
                CryptoPayInvoice.paid_at.label("paid_at"),
            ).where(
                CryptoPayInvoice.project_id == project_id,
                CryptoPayInvoice.telegram_bot_id == bot_id,
                CryptoPayInvoice.telegram_user_id == buyer_id,
                CryptoPayInvoice.kind == "file",
                CryptoPayInvoice.status == "paid",
            ),
        ).subquery()
        total = await session.scalar(select(func.count()).select_from(combined)) or 0
        rows = (await session.execute(
            select(combined.c.id, combined.c.product_name)
            .order_by(combined.c.paid_at.desc(), combined.c.id.desc())
            .offset(page * 8).limit(8)
        )).all()
        return total, tuple(PaidFileListing(id=row[0], product_name=row[1])
                            for row in rows)


async def purchased_assets(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, purchase_id: UUID,
) -> tuple[FilePurchase | FileManualOrder | CryptoPayInvoice, tuple[FileAsset, ...]]:
    """A paid receipt, not membership or public catalog access, unlocks files."""
    async with system_project_session_scope(factory, project_id) as session:
        order = await session.scalar(select(FilePurchase).where(
            FilePurchase.id == purchase_id,
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.status == "paid",
        ))
        if order is None:
            order = await session.scalar(select(FileManualOrder).where(
                FileManualOrder.id == purchase_id,
                FileManualOrder.project_id == project_id,
                FileManualOrder.telegram_bot_id == bot_id,
                FileManualOrder.telegram_user_id == buyer_id,
                FileManualOrder.status == "approved",
            ))
        if order is None:
            order = await session.scalar(select(CryptoPayInvoice).where(
                CryptoPayInvoice.id == purchase_id,
                CryptoPayInvoice.project_id == project_id,
                CryptoPayInvoice.telegram_bot_id == bot_id,
                CryptoPayInvoice.telegram_user_id == buyer_id,
                CryptoPayInvoice.kind == "file",
                CryptoPayInvoice.status == "paid",
            ))
        if order is None:
            raise PermissionError(tr("Оплаченная покупка не найдена", "Paid purchase not found"))
        assets = tuple((await session.scalars(select(FileAsset).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == order.product_id,
        ).order_by(FileAsset.sort_order).limit(ASSET_LIMIT))).all())
        if not assets:
            raise RuntimeError(tr(
                "Файлы временно недоступны. Обратитесь к поддержке",
                "Files are temporarily unavailable. Contact support",
            ))
        return order, assets


async def mark_file_delivery(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, purchase_id: UUID,
) -> None:
    async with system_project_session_scope(factory, project_id) as session:
        order = await session.scalar(select(FilePurchase).where(
            FilePurchase.id == purchase_id,
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.status == "paid",
        ).with_for_update())
        if order is None:
            order = await session.scalar(select(FileManualOrder).where(
                FileManualOrder.id == purchase_id,
                FileManualOrder.project_id == project_id,
                FileManualOrder.telegram_bot_id == bot_id,
                FileManualOrder.telegram_user_id == buyer_id,
                FileManualOrder.status == "approved",
            ).with_for_update())
        if order is None:
            order = await session.scalar(select(CryptoPayInvoice).where(
                CryptoPayInvoice.id == purchase_id,
                CryptoPayInvoice.project_id == project_id,
                CryptoPayInvoice.telegram_bot_id == bot_id,
                CryptoPayInvoice.telegram_user_id == buyer_id,
                CryptoPayInvoice.kind == "file",
                CryptoPayInvoice.status == "paid",
            ).with_for_update())
        if order:
            order.last_delivered_at = datetime.now(UTC)
