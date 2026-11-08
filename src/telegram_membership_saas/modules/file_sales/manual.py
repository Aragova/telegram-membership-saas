"""Manual file-payment flows isolated from subscription checkout.

Buyer funds are verified by an authorized owner/payment manager. An approval
unlocks exactly one file purchase, never a channel membership.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import (
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.file_sales.models import (
    FileAsset,
    FileFolder,
    FileManualPurchase,
    FilePrice,
)
from telegram_membership_saas.modules.file_sales.service import (
    ASSET_LIMIT,
    PENDING_ORDER_LIMIT,
    _load_paid,
    _owner,
    _product,
    _ready,
)
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
    PaymentMethodStatus,
    render_manual_instruction,
)
from telegram_membership_saas.modules.payments.request_service import (
    require_payment_permission,
)
from telegram_membership_saas.modules.pricing import (
    CurrencyAsset,
    CurrencyAssetStatus,
)
from telegram_membership_saas.modules.projects import Permission
from telegram_membership_saas.modules.telegram_commerce.service import (
    _reviewer_telegram_ids,
    format_amount,
    upsert_telegram_user,
)
from telegram_membership_saas.tenant_context import TenantContext

FIAT_CODES = ("RUB", "USD", "EUR", "UAH", "BYN")


@dataclass(frozen=True, slots=True)
class FilePaymentOption:
    currency: str
    amount: Decimal
    precision: int
    method_id: UUID
    method_name: str


def parse_price(value: str, precision: int) -> Decimal:
    try:
        amount = Decimal(value.strip().replace(",", "."))
        if not amount.is_finite() or amount <= 0 or amount > 1_000_000_000:
            raise ValueError("Укажите положительную стоимость товара")
        quantum = Decimal(1).scaleb(-precision)
        if amount != amount.quantize(quantum):
            raise ValueError("Слишком много знаков после запятой")
        return amount
    except (InvalidOperation, ArithmeticError) as exc:
        raise ValueError("Неверная сумма") from exc


async def set_file_price(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, actor_id: UUID, product_id: UUID,
    currency: str, value: str,
) -> None:
    if currency not in FIAT_CODES:
        raise ValueError("Неподдерживаемая валюта")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, actor_id=actor_id,
                     bot_id=bot_id)
        product = await _product(session, project_id, bot_id, product_id, lock=True)
        if product.status not in ("draft", "published"):
            raise ValueError("Товар снят с продажи")
        asset = await session.scalar(select(CurrencyAsset).where(
            CurrencyAsset.code == currency,
            CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
        ))
        if asset is None:
            raise ValueError("Валюта пока недоступна")
        amount = parse_price(value, min(asset.precision, 4))
        price = await session.scalar(select(FilePrice).where(
            FilePrice.project_id == project_id,
            FilePrice.product_id == product_id,
            FilePrice.currency_code == currency,
        ).with_for_update())
        if price:
            price.amount = amount
        else:
            session.add(FilePrice(
                project_id=project_id, product_id=product_id,
                currency_code=currency, amount=amount,
            ))


async def list_file_prices(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, product_id: UUID,
    owner: bool = False,
) -> tuple[tuple[str, Decimal, int], ...]:
    async with system_project_session_scope(factory, project_id) as session:
        item = await _product(session, project_id, bot_id, product_id)
        if not owner and item.status != "published":
            raise ValueError("Товар не продаётся")
        rows = (await session.execute(
            select(FilePrice.currency_code, FilePrice.amount, CurrencyAsset.precision)
            .join(CurrencyAsset, CurrencyAsset.code == FilePrice.currency_code)
            .where(
                FilePrice.project_id == project_id,
                FilePrice.product_id == product_id,
                CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
            ).order_by(FilePrice.currency_code)
        )).all()
        return tuple((currency, amount, precision) for currency, amount, precision in rows)


async def list_file_methods(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, product_id: UUID,
) -> tuple[FilePaymentOption, ...]:
    async with system_project_session_scope(factory, project_id) as session:
        await _ready(session, project_id=project_id, bot_id=bot_id)
        item = await _product(session, project_id, bot_id, product_id)
        if item.status != "published":
            raise ValueError("Товар недоступен")
        rows = (await session.execute(
            select(FilePrice.currency_code, FilePrice.amount,
                   CurrencyAsset.precision, PaymentMethod.id, PaymentMethod.name)
            .join(CurrencyAsset, CurrencyAsset.code == FilePrice.currency_code)
            .join(PaymentMethodCurrency,
                  PaymentMethodCurrency.project_id == FilePrice.project_id)
            .join(PaymentMethod, PaymentMethod.id ==
                  PaymentMethodCurrency.payment_method_id)
            .where(
                FilePrice.project_id == project_id,
                FilePrice.product_id == product_id,
                PaymentMethodCurrency.currency_code == FilePrice.currency_code,
                PaymentMethod.project_id == project_id,
                PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
                CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
            ).order_by(PaymentMethod.sort_order, PaymentMethod.name)
        )).all()
        return tuple(FilePaymentOption(
            currency=currency, amount=amount, precision=precision,
            method_id=method_id, method_name=name,
        ) for currency, amount, precision, method_id, name in rows)


async def create_manual_file_order(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, product_id: UUID,
    currency: str, method_id: UUID,
) -> FileManualPurchase:
    async with system_project_session_scope(factory, project_id) as session:
        await _ready(session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id)
        product = await _product(session, project_id, bot_id, product_id, lock=True)
        if product.status != "published":
            raise ValueError("Товар недоступен")
        folder = await session.scalar(select(FileFolder.id).where(
            FileFolder.id == product.folder_id,
            FileFolder.project_id == project_id, FileFolder.status == "active",
        ))
        assets = await session.scalar(select(func.count(FileAsset.id)).where(
            FileAsset.project_id == project_id, FileAsset.product_id == product_id,
        ))
        if folder is None or not assets:
            raise ValueError("Файл временно недоступен")
        matching_price = await session.scalar(select(FilePrice).where(
            FilePrice.product_id == product_id,
            FilePrice.project_id == project_id,
            FilePrice.currency_code == currency,
        ))
        method = await session.scalar(
            select(PaymentMethod).join(
                PaymentMethodCurrency,
                PaymentMethodCurrency.payment_method_id == PaymentMethod.id,
            ).where(
                PaymentMethod.project_id == project_id,
                PaymentMethod.id == method_id,
                PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
                PaymentMethodCurrency.project_id == project_id,
                PaymentMethodCurrency.currency_code == currency,
            )
        )
        asset = await session.scalar(select(CurrencyAsset).where(
            CurrencyAsset.code == currency,
            CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
        ))
        if not matching_price or not method or not asset:
            raise ValueError("Цена или способ оплаты изменились")
        already = await session.scalar(select(FileManualPurchase).where(
            FileManualPurchase.project_id == project_id,
            FileManualPurchase.telegram_bot_id == bot_id,
            FileManualPurchase.telegram_user_id == buyer_id,
            FileManualPurchase.product_id == product_id,
            FileManualPurchase.status == "paid",
        ).limit(1))
        if already:
            return already
        stars_paid = await _load_paid(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
        )
        if stars_paid:
            raise ValueError("Товар уже куплен за Stars. Откройте /myfiles")
        # Only one transfer may await review for a product and buyer.
        # A second method would otherwise create overlapping approvals.
        reviewing = await session.scalar(select(FileManualPurchase.id).where(
            FileManualPurchase.project_id == project_id,
            FileManualPurchase.telegram_bot_id == bot_id,
            FileManualPurchase.telegram_user_id == buyer_id,
            FileManualPurchase.product_id == product_id,
            FileManualPurchase.status == "waiting_review",
        ).limit(1))
        if reviewing:
            raise ValueError("Эта покупка уже ожидает проверки чека")
        previous = await session.scalar(select(FileManualPurchase).where(
            FileManualPurchase.project_id == project_id,
            FileManualPurchase.telegram_bot_id == bot_id,
            FileManualPurchase.telegram_user_id == buyer_id,
            FileManualPurchase.product_id == product_id,
            FileManualPurchase.currency_code == currency,
            FileManualPurchase.payment_method_id == method_id,
            FileManualPurchase.amount == matching_price.amount,
            FileManualPurchase.status.in_(("waiting_confirmation", "waiting_review")),
        ).order_by(FileManualPurchase.created_at.desc()).limit(1))
        if previous:
            return previous
        open_orders = await session.scalar(select(func.count(FileManualPurchase.id)).where(
            FileManualPurchase.project_id == project_id,
            FileManualPurchase.telegram_bot_id == bot_id,
            FileManualPurchase.telegram_user_id == buyer_id,
            FileManualPurchase.status.in_(("waiting_confirmation", "waiting_review")),
        )) or 0
        if open_orders >= PENDING_ORDER_LIMIT:
            raise ValueError("Слишком много незавершённых оплат")
        instruction = render_manual_instruction(
            method.instruction_template,
            amount=format_amount(matching_price.amount, min(asset.precision, 4)),
            currency=currency, plan=product.name, period="",
        )
        order = FileManualPurchase(
            project_id=project_id, product_id=product_id,
            telegram_bot_id=bot_id, telegram_user_id=buyer_id,
            product_name=product.name, amount=matching_price.amount,
            currency_code=currency, payment_method_id=method_id,
            payment_method_name=method.name, instruction=instruction,
            status="waiting_confirmation",
        )
        session.add(order)
        await session.flush()
        return order


async def _manual_for_buyer(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    buyer_id: int, order_id: UUID, lock: bool = False,
) -> FileManualPurchase:
    query = select(FileManualPurchase).where(
        FileManualPurchase.project_id == project_id,
        FileManualPurchase.telegram_bot_id == bot_id,
        FileManualPurchase.telegram_user_id == buyer_id,
        FileManualPurchase.id == order_id,
    )
    order = await session.scalar(query.with_for_update() if lock else query)
    if order is None:
        raise PermissionError("Счёт не принадлежит этому покупателю")
    return order


async def require_manual_receipt(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, order_id: UUID,
) -> None:
    async with system_project_session_scope(factory, project_id) as session:
        await _ready(session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id)
        order = await _manual_for_buyer(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, order_id=order_id,
        )
        if order.status not in ("waiting_confirmation", "waiting_review"):
            raise ValueError("Счёт уже закрыт")


async def submit_manual_receipt(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, order_id: UUID,
) -> tuple[FileManualPurchase, tuple[int, ...]]:
    async with system_project_session_scope(factory, project_id) as session:
        await _ready(session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id)
        # Serialize competing receipts for the same file/buyer.
        initial = await _manual_for_buyer(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, order_id=order_id,
        )
        await _product(session, project_id, bot_id, initial.product_id, lock=True)
        order = await _manual_for_buyer(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, order_id=order_id, lock=True,
        )
        if order.status not in ("waiting_confirmation", "waiting_review"):
            raise ValueError("Счёт уже закрыт")
        if order.status == "waiting_confirmation":
            sibling = await session.scalar(select(FileManualPurchase.id).where(
                FileManualPurchase.project_id == project_id,
                FileManualPurchase.telegram_bot_id == bot_id,
                FileManualPurchase.telegram_user_id == buyer_id,
                FileManualPurchase.product_id == order.product_id,
                FileManualPurchase.id != order.id,
                FileManualPurchase.status.in_(("paid", "waiting_review")),
            ).limit(1))
            if sibling:
                raise ValueError("Этот файл уже оплачен или другой чек ожидает проверки")
            siblings = (await session.scalars(select(FileManualPurchase).where(
                FileManualPurchase.project_id == project_id,
                FileManualPurchase.telegram_bot_id == bot_id,
                FileManualPurchase.telegram_user_id == buyer_id,
                FileManualPurchase.product_id == order.product_id,
                FileManualPurchase.id != order.id,
                FileManualPurchase.status == "waiting_confirmation",
            ).with_for_update())).all()
            for superseded in siblings:
                superseded.status = "cancelled"
            order.status = "waiting_review"
            order.submitted_at = datetime.now(UTC)
        await session.flush()
        reviewers = await _reviewer_telegram_ids(session, project_id=project_id)
        return order, reviewers


async def mark_manual_receipt_forwarded(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, order_id: UUID,
) -> None:
    async with system_project_session_scope(factory, project_id) as session:
        order = await _manual_for_buyer(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, order_id=order_id, lock=True,
        )
        if order.status == "waiting_review" and order.receipt_forwarded_at is None:
            order.receipt_forwarded_at = datetime.now(UTC)


async def cancel_manual_order(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, order_id: UUID,
) -> None:
    async with system_project_session_scope(factory, project_id) as session:
        order = await _manual_for_buyer(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, order_id=order_id, lock=True,
        )
        if order.status != "waiting_confirmation":
            raise ValueError("Заявка уже отправлена или обработана")
        order.status = "cancelled"


async def review_manual_file_order(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, reviewer_id: int, reviewer_name: str | None,
    reviewer_first_name: str | None, order_id: UUID,
    approve: bool,
) -> tuple[FileManualPurchase, bool]:
    async with system_project_session_scope(factory, project_id) as session:
        reviewer = await upsert_telegram_user(
            session, telegram_user_id=reviewer_id,
            username=reviewer_name, first_name=reviewer_first_name,
        )
        await require_payment_permission(
            session, project_id=project_id, actor_user_id=reviewer.id,
            permission=Permission.PAYMENT_APPROVE if approve else Permission.PAYMENT_REJECT,
        )
        # All buyer/reviewer mutations lock the product before the order.
        # Reading the immutable product id is safe without FOR UPDATE; we
        # re-fetch and lock the order below before checking/changing its state.
        initial = await session.scalar(select(FileManualPurchase).where(
            FileManualPurchase.project_id == project_id,
            FileManualPurchase.telegram_bot_id == bot_id,
            FileManualPurchase.id == order_id,
        ))
        if initial is None:
            raise ValueError("Счёт не найден")
        await _product(
            session, project_id, bot_id, initial.product_id, lock=True,
        )
        order = await session.scalar(select(FileManualPurchase).where(
            FileManualPurchase.project_id == project_id,
            FileManualPurchase.telegram_bot_id == bot_id,
            FileManualPurchase.id == order_id,
        ).execution_options(populate_existing=True).with_for_update())
        if order is None:
            raise ValueError("Счёт не найден")
        target = "paid" if approve else "rejected"
        if order.status == target:
            return order, False
        if order.status != "waiting_review" or order.receipt_forwarded_at is None:
            raise ValueError("Счёт не ожидает проверки или чек не доставлен")
        if approve:
            # Product and order locks already held in the canonical order.
            # Also check a Stars charge on the other payment rail.
            stars_paid = await _load_paid(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=order.telegram_user_id, product_id=order.product_id,
            )
            if stars_paid:
                raise ValueError("Этот файл уже оплачен через Stars")
            manual_paid = await session.scalar(select(FileManualPurchase.id).where(
                FileManualPurchase.project_id == project_id,
                FileManualPurchase.telegram_bot_id == bot_id,
                FileManualPurchase.telegram_user_id == order.telegram_user_id,
                FileManualPurchase.product_id == order.product_id,
                FileManualPurchase.id != order.id,
                FileManualPurchase.status == "paid",
            ).limit(1))
            if manual_paid:
                raise ValueError("Этот файл уже оплачен другим способом")
        order.status = target
        order.reviewed_by = reviewer.id
        if approve:
            order.paid_at = datetime.now(UTC)
        await session.flush()
        return order, True


async def pending_manual_reviews(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, reviewer_id: int, reviewer_name: str | None,
    reviewer_first_name: str | None,
) -> tuple[FileManualPurchase, ...]:
    async with system_project_session_scope(factory, project_id) as session:
        reviewer = await upsert_telegram_user(
            session, telegram_user_id=reviewer_id,
            username=reviewer_name, first_name=reviewer_first_name,
        )
        await require_payment_permission(
            session, project_id=project_id, actor_user_id=reviewer.id,
            permission=Permission.PAYMENT_VIEW,
        )
        return tuple((await session.scalars(
            select(FileManualPurchase).where(
                FileManualPurchase.project_id == project_id,
                FileManualPurchase.telegram_bot_id == bot_id,
                FileManualPurchase.status == "waiting_review",
                FileManualPurchase.receipt_forwarded_at.is_not(None),
            ).order_by(FileManualPurchase.submitted_at, FileManualPurchase.id)
            .limit(30)
        )).all())


async def list_manual_paid(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, page: int = 0,
) -> tuple[int, tuple[FileManualPurchase, ...]]:
    if not 0 <= page <= 10000:
        raise ValueError("Неверная страница")
    async with system_project_session_scope(factory, project_id) as session:
        conditions = (
            FileManualPurchase.project_id == project_id,
            FileManualPurchase.telegram_bot_id == bot_id,
            FileManualPurchase.telegram_user_id == buyer_id,
            FileManualPurchase.status == "paid",
        )
        total = await session.scalar(
            select(func.count(FileManualPurchase.id)).where(*conditions)
        ) or 0
        rows = (await session.scalars(
            select(FileManualPurchase).where(*conditions)
            .order_by(FileManualPurchase.paid_at.desc(), FileManualPurchase.id.desc())
            .offset(page * 8).limit(8)
        )).all()
        return total, tuple(rows)


async def purchased_manual_assets(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, order_id: UUID,
) -> tuple[FileManualPurchase, tuple[FileAsset, ...]]:
    async with system_project_session_scope(factory, project_id) as session:
        order = await _manual_for_buyer(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, order_id=order_id,
        )
        if order.status != "paid":
            raise PermissionError("Оплата ещё не подтверждена")
        assets = tuple((await session.scalars(
            select(FileAsset).where(
                FileAsset.project_id == project_id,
                FileAsset.product_id == order.product_id,
            ).order_by(FileAsset.sort_order).limit(ASSET_LIMIT)
        )).all())
        if not assets:
            raise ValueError("Файлы недоступны, обратитесь в поддержку")
        return order, assets


async def mark_manual_delivery(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, order_id: UUID,
) -> None:
    async with system_project_session_scope(factory, project_id) as session:
        order = await _manual_for_buyer(
            session, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, order_id=order_id, lock=True,
        )
        if order.status == "paid":
            order.last_delivered_at = datetime.now(UTC)
