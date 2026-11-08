"""Tenant-restricted digital products and Telegram Stars checkout.

A paid file order does not produce a channel Subscription or change the
existing manual-settlement Payment/PaymentRequest/Entitlement ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import (
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots.models import BotInstance, BotInstanceStatus
from telegram_membership_saas.modules.file_sales.models import (
    FileAsset,
    FileFolder,
    FileManualPurchase,
    FileProduct,
    FilePurchase,
)
from telegram_membership_saas.modules.identity.models import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.projects.models import Project, ProjectStatus
from telegram_membership_saas.modules.telegram_admin.service import (
    _require_project_manager,
)
from telegram_membership_saas.tenant_context import TenantContext

ASSET_LIMIT = 20
FOLDER_LIMIT = 100
PRODUCT_LIMIT = 2000
PENDING_ORDER_LIMIT = 50


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
        raise PermissionError("Этот бот отключён")


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
        raise PermissionError("Магазин недоступен")
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
            raise PermissionError("Покупки в этом боте заблокированы")


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
        q = select(FileFolder).where(FileFolder.project_id == project_id)
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
        raise ValueError("Название папки: от 1 до 100 символов")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        count = await session.scalar(select(func.count(FileFolder.id)).where(
            FileFolder.project_id == project_id,
        ))
        if count >= FOLDER_LIMIT:
            raise ValueError("Достигнут лимит папок")
        folder = FileFolder(project_id=project_id, name=name, status="active")
        session.add(folder)
        await session.flush()
        return folder


async def create_product(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID, folder_id: UUID,
    name: str, stars: int, description: str = "",
) -> FileProduct:
    name = name.strip()
    if not 1 <= len(name) <= 120:
        raise ValueError("Название товара: от 1 до 120 символов")
    if not 0 <= stars <= 250000:
        raise ValueError("Цена Stars: 0 (отключено) или от 1 до 250000")
    if len(description) > 1024:
        raise ValueError("Описание: не более 1024 символов")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        folder = await session.scalar(select(FileFolder.id).where(
            FileFolder.id == folder_id,
            FileFolder.project_id == project_id,
            FileFolder.status == "active",
        ))
        if folder is None:
            raise ValueError("Папка недоступна")
        total = await session.scalar(select(func.count(FileProduct.id)).where(
            FileProduct.project_id == project_id,
        ))
        if total >= PRODUCT_LIMIT:
            raise ValueError("Достигнут лимит товаров")
        product = FileProduct(
            project_id=project_id, folder_id=folder_id,
            telegram_bot_id=bot_id, name=name, stars_price=stars,
            description=description, status="draft",
        )
        session.add(product)
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
        raise ValueError("Товар не найден в этом боте")
    return product


async def update_draft_product(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
    product_id: UUID, field: str, value: str,
) -> FileProduct:
    if field not in ("name", "description", "stars_price"):
        raise ValueError("Недоступное поле товара")
    if field == "name":
        value = value.strip()
        if not 1 <= len(value) <= 120:
            raise ValueError("Название товара: от 1 до 120 символов")
    elif field == "description":
        value = value.strip()
        if len(value) > 1024:
            raise ValueError("Описание: не более 1024 символов")
    else:
        if not value.isascii() or not value.isdecimal():
            raise ValueError("Цена должна быть целым числом Stars")
        if not 0 <= int(value) <= 250000:
            raise ValueError("Цена Stars: 0 (отключено) или от 1 до 250000")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id,
                     actor_id=actor_id, bot_id=bot_id)
        product = await _product(
            session, project_id, bot_id, product_id, lock=True,
        )
        if product.status != "draft":
            raise ValueError("Редактировать можно только черновик")
        setattr(product, field, int(value) if field == "stars_price" else value)
        await session.flush()
        return product


async def add_product_asset(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
    product_id: UUID, kind: str, file_id: str,
) -> int:
    if kind not in ("document", "photo", "video", "audio", "animation"):
        raise ValueError("Неподдерживаемый тип файла")
    if not 1 <= len(file_id) <= 512:
        raise ValueError("Недопустимый Telegram файл")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        product = await _product(
            session, project_id, bot_id, product_id, lock=True,
        )
        if product.status != "draft":
            raise ValueError("Добавление файлов возможно только в черновик")
        count = await session.scalar(select(func.count(FileAsset.id)).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
        )) or 0
        if count >= ASSET_LIMIT:
            raise ValueError("Не более 20 вложений в одном товаре")
        session.add(FileAsset(
            project_id=project_id, product_id=product_id,
            kind=kind, file_id=file_id, sort_order=count,
        ))
        return count + 1


async def change_product_status(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
    product_id: UUID, status: str,
) -> None:
    if status not in ("published", "archived"):
        raise ValueError("Недопустимый статус товара")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        item = await _product(session, project_id, bot_id, product_id, lock=True)
        if status == "published":
            assets = await session.scalar(select(func.count(FileAsset.id)).where(
                FileAsset.project_id == project_id,
                FileAsset.product_id == product_id,
            )) or 0
            if not assets:
                raise ValueError("Сначала прикрепите файлы")
            # A file must have at least one valid payment price.
            if item.stars_price == 0:
                from telegram_membership_saas.modules.file_sales.models import FilePrice
                priced = await session.scalar(select(FilePrice.product_id).where(
                    FilePrice.project_id == project_id,
                    FilePrice.product_id == product_id,
                ).limit(1))
                if priced is None:
                    raise ValueError("Укажите цену Stars или цену в валюте")
            folder = await session.scalar(select(FileFolder.id).where(
                FileFolder.project_id == project_id,
                FileFolder.id == item.folder_id,
                FileFolder.status == "active",
            ))
            if folder is None:
                raise ValueError("Папка архивирована")
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
        )
        if not actor_id:
            folder = await session.scalar(select(FileFolder.id).where(
                FileFolder.id == folder_id,
                FileFolder.project_id == project_id,
                FileFolder.status == "active",
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
        if actor_id is None and product.status != "published":
            raise ValueError("Товар не продаётся")
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


async def create_stars_order(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, product_id: UUID,
) -> DigitalOrder:
    async with system_project_session_scope(factory, project_id) as session:
        await _ready(
            session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id,
        )
        item = await _product(session, project_id, bot_id, product_id, lock=True)
        if item.status != "published":
            raise ValueError("Товар больше не продаётся")
        if item.stars_price <= 0:
            raise ValueError("Оплата Stars для товара отключена")
        folder = await session.scalar(select(FileFolder.id).where(
            FileFolder.project_id == project_id, FileFolder.id == item.folder_id,
            FileFolder.status == "active",
        ))
        if folder is None:
            raise ValueError("Товар больше не продаётся")
        count = await session.scalar(select(func.count(FileAsset.id)).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == product_id,
        )) or 0
        if count == 0:
            raise ValueError("Товар не содержит файлов")
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
        # A different payment rail must not charge the same product twice.
        manual = await session.scalar(select(FileManualPurchase.id).where(
            FileManualPurchase.project_id == project_id,
            FileManualPurchase.telegram_bot_id == bot_id,
            FileManualPurchase.telegram_user_id == buyer_id,
            FileManualPurchase.product_id == product_id,
            FileManualPurchase.status.in_(("paid", "waiting_review")),
        ).limit(1))
        if manual:
            raise ValueError(
                "По этому файлу уже подтверждена или проверяется ручная оплата. "
                "Откройте /myfiles или дождитесь ответа администратора."
            )
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
            raise ValueError("Слишком много незавершённых покупок")
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
            manual = await session.scalar(select(FileManualPurchase.id).where(
                FileManualPurchase.project_id == project_id,
                FileManualPurchase.telegram_bot_id == bot_id,
                FileManualPurchase.telegram_user_id == buyer_id,
                FileManualPurchase.product_id == purchase.product_id,
                FileManualPurchase.status.in_(("paid", "waiting_review")),
            ).limit(1))
            if manual:
                return False
            item = await _product(session, project_id, bot_id, purchase.product_id)
            return item.status == "published" and (
                await session.scalar(select(func.count(FileAsset.id)).where(
                    FileAsset.project_id == project_id,
                    FileAsset.product_id == purchase.product_id,
                )) or 0
            ) > 0
        except (PermissionError, ValueError):
            return False


async def confirm_stars_payment(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int,
    invoice_payload: str, amount: int, currency: str,
    charge_id: str,
) -> FilePurchase:
    if currency != "XTR" or not charge_id or not invoice_payload.startswith("fs:"):
        raise ValueError("Это не платёж за цифровой товар")
    async with system_project_session_scope(factory, project_id) as session:
        original = await session.scalar(select(FilePurchase).where(
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.invoice_payload == invoice_payload,
        ))
        if original is None or amount != original.stars_amount:
            raise PermissionError("Платёж не соответствует заказу этого бота")
        # Serialize a charged Stars callback with manual approval through
        # the product row. This prevents both rails simultaneously granting.
        await _product(session, project_id, bot_id, original.product_id, lock=True)
        purchase = await session.scalar(select(FilePurchase).where(
            FilePurchase.id == original.id,
            FilePurchase.project_id == project_id,
        ).with_for_update())
        if purchase.telegram_charge_id and purchase.telegram_charge_id != charge_id:
            raise PermissionError("Заказ уже связан с другим платежом")
        if purchase.status in ("paid", "refund_pending", "refunded"):
            return purchase
        if purchase.status != "pending":
            raise ValueError("Неверное состояние заказа")
        manual_paid = await session.scalar(select(FileManualPurchase.id).where(
            FileManualPurchase.project_id == project_id,
            FileManualPurchase.telegram_bot_id == bot_id,
            FileManualPurchase.telegram_user_id == buyer_id,
            FileManualPurchase.product_id == purchase.product_id,
            FileManualPurchase.status == "paid",
        ).limit(1))
        purchase.telegram_charge_id = charge_id
        purchase.status = "refund_pending" if manual_paid else "paid"
        purchase.paid_at = datetime.now(UTC)
        await session.flush()
        return purchase


async def mark_refunded_stars(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, purchase_id: UUID, charge_id: str,
) -> None:
    """Record successful Telegram refund; never unlock refunded media."""
    async with system_project_session_scope(factory, project_id) as session:
        purchase = await session.scalar(select(FilePurchase).where(
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.id == purchase_id,
        ).with_for_update())
        if purchase is None or purchase.telegram_charge_id != charge_id:
            raise PermissionError("Платёж для возврата не найден")
        if purchase.status == "refunded":
            return
        if purchase.status != "refund_pending":
            raise ValueError("Заказ не ожидает возврата")
        purchase.status = "refunded"


async def pending_stars_refunds(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int,
) -> tuple[FilePurchase, ...]:
    async with system_project_session_scope(factory, project_id) as session:
        return tuple((await session.scalars(select(FilePurchase).where(
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.status == "refund_pending",
        ).limit(20))).all())


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
) -> tuple[int, tuple[FilePurchase, ...]]:
    if not 0 <= page <= 10000:
        raise ValueError("Неверная страница")
    async with system_project_session_scope(factory, project_id) as session:
        where = (
            FilePurchase.project_id == project_id,
            FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id,
            FilePurchase.status == "paid",
        )
        total = await session.scalar(select(func.count(FilePurchase.id)).where(*where)) or 0
        rows = (await session.scalars(
            select(FilePurchase).where(*where)
            .order_by(FilePurchase.paid_at.desc(), FilePurchase.id.desc())
            .offset(page * 8).limit(8)
        )).all()
        return total, tuple(rows)


async def purchased_assets(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, purchase_id: UUID,
) -> tuple[FilePurchase, tuple[FileAsset, ...]]:
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
            raise PermissionError("Оплаченная покупка не найдена")
        assets = tuple((await session.scalars(select(FileAsset).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == order.product_id,
        ).order_by(FileAsset.sort_order).limit(ASSET_LIMIT))).all())
        if not assets:
            raise RuntimeError("Файлы временно недоступны. Обратитесь к поддержке")
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
        if order:
            order.last_delivered_at = datetime.now(UTC)
