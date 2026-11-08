"""Manual file checkout using the project's existing payment methods.

No PaymentRequest, Subscription or Entitlement is created for a file order.
All amounts and instructions are snapshotted at checkout, and only a reviewer
with PAYMENT_APPROVE can release the file purchase.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope, tenant_session_scope
from telegram_membership_saas.modules.file_sales.manual_models import (
    FileManualOrder,
    FileManualPrice,
)
from telegram_membership_saas.modules.file_sales.models import (
    FileAsset,
    FileFolder,
    FilePurchase,
)
from telegram_membership_saas.modules.file_sales.service import _owner, _product, _ready
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
    PaymentMethodStatus,
    render_manual_instruction,
)
from telegram_membership_saas.modules.payments.request_service import require_payment_permission
from telegram_membership_saas.modules.pricing import CurrencyAsset, CurrencyAssetStatus
from telegram_membership_saas.modules.projects import Permission, Project, ProjectMember
from telegram_membership_saas.modules.telegram_commerce.service import format_amount
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class ManualMethod:
    currency: str
    amount: Decimal
    precision: int
    method_id: UUID
    method_name: str


def money(amount: Decimal, precision: int) -> str:
    return format_amount(amount, precision)


async def _methods(
    session: AsyncSession, *, project_id: UUID, product_id: UUID,
) -> tuple[ManualMethod, ...]:
    rows = (await session.execute(
        select(FileManualPrice, CurrencyAsset, PaymentMethod)
        .join(
            CurrencyAsset, CurrencyAsset.code == FileManualPrice.currency_code,
        )
        .join(
            PaymentMethodCurrency,
            and_(
                PaymentMethodCurrency.project_id == FileManualPrice.project_id,
                PaymentMethodCurrency.currency_code == FileManualPrice.currency_code,
            ),
        )
        .join(
            PaymentMethod,
            and_(
                PaymentMethod.id == PaymentMethodCurrency.payment_method_id,
                PaymentMethod.project_id == FileManualPrice.project_id,
            ),
        )
        .where(
            FileManualPrice.project_id == project_id,
            FileManualPrice.product_id == product_id,
            PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
            CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
        )
        .order_by(CurrencyAsset.code, PaymentMethod.sort_order, PaymentMethod.name)
    )).all()
    return tuple(ManualMethod(
        currency=price.currency_code, amount=price.amount,
        precision=currency.precision, method_id=method.id,
        method_name=method.name,
    ) for price, currency, method in rows)


async def available_methods(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, product_id: UUID, buyer_id: int,
) -> tuple[ManualMethod, ...]:
    async with system_project_session_scope(factory, project_id) as session:
        await _ready(session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id)
        product = await _product(session, project_id, bot_id, product_id)
        if product.status != "published":
            raise ValueError("Товар снят с продажи")
        return await _methods(session, project_id=project_id, product_id=product_id)


async def can_review_file_payments(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, actor_id: UUID,
) -> bool:
    """Check payment-view role independently of file product admin access."""
    async with system_project_session_scope(factory, project_id) as session:
        try:
            await require_payment_permission(
                session, project_id=project_id, actor_user_id=actor_id,
                permission=Permission.PAYMENT_VIEW,
            )
        except PermissionError:
            return False
        return True


async def available_price_currencies(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
) -> tuple[str, ...]:
    """Only currently enabled manual payment currencies are offered at creation."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, bot_id=bot_id, actor_id=actor_id)
        return tuple((await session.scalars(
            select(PaymentMethodCurrency.currency_code)
            .join(
                PaymentMethod,
                and_(PaymentMethod.id == PaymentMethodCurrency.payment_method_id,
                     PaymentMethod.project_id == PaymentMethodCurrency.project_id),
            )
            .join(CurrencyAsset, CurrencyAsset.code == PaymentMethodCurrency.currency_code)
            .where(
                PaymentMethodCurrency.project_id == project_id,
                PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
                CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
            )
            .distinct()
            .order_by(PaymentMethodCurrency.currency_code)
        )).all())


async def price_editor_options(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, product_id: UUID, actor_id: UUID,
) -> tuple[tuple[str, Decimal | None], ...]:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, bot_id=bot_id, actor_id=actor_id)
        await _product(session, project_id, bot_id, product_id)
        codes = (await session.scalars(
            select(PaymentMethodCurrency.currency_code)
            .join(
                PaymentMethod,
                and_(PaymentMethod.id == PaymentMethodCurrency.payment_method_id,
                     PaymentMethod.project_id == PaymentMethodCurrency.project_id),
            ).join(CurrencyAsset, CurrencyAsset.code == PaymentMethodCurrency.currency_code)
            .where(
                PaymentMethodCurrency.project_id == project_id,
                PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
                CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
            ).distinct().order_by(PaymentMethodCurrency.currency_code)
        )).all()
        prices = (await session.scalars(
            select(FileManualPrice).where(
                FileManualPrice.project_id == project_id,
                FileManualPrice.product_id == product_id,
            )
        )).all()
        values = {p.currency_code: p.amount for p in prices}
        return tuple((code, values.get(code)) for code in codes)


async def set_manual_price(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, product_id: UUID, actor_id: UUID,
    currency: str, value: str,
) -> Decimal:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _owner(session, project_id=project_id, bot_id=bot_id, actor_id=actor_id)
        item = await _product(session, project_id, bot_id, product_id, lock=True)
        if item.status == "archived":
            raise ValueError("Сначала верните товар в продажу")
        asset = await session.scalar(select(CurrencyAsset).where(
            CurrencyAsset.code == currency,
            CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
        ))
        if asset is None:
            raise ValueError("Валюта недоступна")
        valid_method = await session.scalar(
            select(PaymentMethod.id)
            .join(
                PaymentMethodCurrency,
                and_(PaymentMethodCurrency.project_id == PaymentMethod.project_id,
                     PaymentMethodCurrency.payment_method_id == PaymentMethod.id),
            )
            .where(
                PaymentMethod.project_id == project_id,
                PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
                PaymentMethodCurrency.currency_code == currency,
            ).limit(1)
        )
        if valid_method is None:
            raise ValueError("Нет активного способа оплаты для этой валюты")
        try:
            amount = Decimal(value.strip().replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError("Укажите числовую цену") from exc
        if not amount.is_finite() or amount <= 0 or amount >= Decimal("1000000000000"):
            raise ValueError("Цена должна быть положительной")
        quantum = Decimal(1).scaleb(-asset.precision)
        with localcontext() as ctx:
            ctx.prec = 50
            if amount != amount.quantize(quantum):
                raise ValueError("Слишком много знаков после запятой для этой валюты")
        record = await session.scalar(select(FileManualPrice).where(
            FileManualPrice.project_id == project_id,
            FileManualPrice.product_id == product_id,
            FileManualPrice.currency_code == currency,
        ).with_for_update())
        if record:
            record.amount = amount
        else:
            session.add(FileManualPrice(
                project_id=project_id, product_id=product_id,
                currency_code=currency, amount=amount,
            ))
        return amount


async def create_manual_order(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, product_id: UUID,
    currency: str, method_id: UUID,
) -> tuple[FileManualOrder | FilePurchase, bool]:
    async with system_project_session_scope(factory, project_id) as session:
        # Serialize manual checkout against transfer finalization and bot retirement.
        await checkout_retirement_lock(session, project_id)
        await _ready(session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id)
        product = await _product(session, project_id, bot_id, product_id, lock=True)
        if product.status != "published":
            raise ValueError("Товар не продаётся")
        folder = await session.scalar(select(FileFolder.id).where(
            FileFolder.id == product.folder_id, FileFolder.project_id == project_id,
            FileFolder.status == "active",
        ))
        count = await session.scalar(select(func.count(FileAsset.id)).where(
            FileAsset.product_id == product_id, FileAsset.project_id == project_id,
        ))
        if folder is None or not count:
            raise ValueError("Файлы пока недоступны")
        stars_paid = await session.scalar(select(FilePurchase).where(
            FilePurchase.project_id == project_id, FilePurchase.telegram_bot_id == bot_id,
            FilePurchase.telegram_user_id == buyer_id, FilePurchase.product_id == product_id,
            FilePurchase.status == "paid",
        ).limit(1))
        if stars_paid:
            return stars_paid, True
        approved = await session.scalar(select(FileManualOrder).where(
            FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.telegram_user_id == buyer_id,
            FileManualOrder.product_id == product_id,
            FileManualOrder.status == "approved",
        ).limit(1))
        if approved:
            return approved, True
        options = await _methods(session, project_id=project_id, product_id=product_id)
        chosen = next((x for x in options if
                       x.currency == currency and x.method_id == method_id), None)
        if chosen is None:
            raise ValueError("Способ оплаты или цена больше недоступны")
        old = await session.scalar(select(FileManualOrder).where(
            FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.telegram_user_id == buyer_id,
            FileManualOrder.product_id == product_id,
            FileManualOrder.status.in_(("pending", "submitted")),
            FileManualOrder.currency_code == currency,
            FileManualOrder.payment_method_id == method_id,
            FileManualOrder.amount == chosen.amount,
        ).order_by(FileManualOrder.created_at.desc()).limit(1))
        if old:
            return old, False
        method = await session.scalar(select(PaymentMethod).where(
            PaymentMethod.id == method_id, PaymentMethod.project_id == project_id,
            PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
        ))
        if method is None:
            raise ValueError("Способ оплаты отключён")
        instructions = render_manual_instruction(
            method.instruction_template,
            amount=money(chosen.amount, chosen.precision),
            currency=currency, plan=product.name, period="",
        )
        pending_count = await session.scalar(select(func.count(FileManualOrder.id)).where(
            FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.telegram_user_id == buyer_id,
            FileManualOrder.status.in_(("pending", "submitted")),
        )) or 0
        if pending_count >= 50:
            raise ValueError("Слишком много незавершённых покупок")
        order = FileManualOrder(
            project_id=project_id, telegram_bot_id=bot_id, telegram_user_id=buyer_id,
            product_id=product_id, product_name=product.name,
            amount=chosen.amount, currency_code=currency,
            payment_method_id=method.id, payment_method_name=method.name,
            instruction=instructions, status="pending",
        )
        session.add(order)
        await session.flush()
        return order, False


async def require_buyer_order(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, order_id: UUID,
) -> FileManualOrder:
    async with system_project_session_scope(factory, project_id) as session:
        row = await session.scalar(select(FileManualOrder).where(
            FileManualOrder.id == order_id, FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.telegram_user_id == buyer_id,
        ))
        if row is None:
            raise PermissionError("Это не ваша покупка")
        return row


async def submit_manual_receipt(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, order_id: UUID,
    kind: str, file_id: str,
) -> tuple[FileManualOrder, tuple[int, ...]]:
    if kind not in ("photo", "document") or not file_id:
        raise ValueError("Прикрепите фото чека или документ")
    async with system_project_session_scope(factory, project_id) as session:
        await _ready(session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id)
        order = await session.scalar(select(FileManualOrder).where(
            FileManualOrder.id == order_id, FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.telegram_user_id == buyer_id,
        ).with_for_update())
        if order is None:
            raise PermissionError("Это не ваша покупка")
        if order.status == "pending":
            order.status = "submitted"
            order.receipt_kind = kind
            order.receipt_file_id = file_id
            order.submitted_at = datetime.now(UTC)
        elif order.status != "submitted":
            raise ValueError("Покупка уже рассмотрена или отменена")
        owner = await session.scalar(
            select(TelegramUser.telegram_user_id)
            .join(Project, Project.owner_user_id == TelegramUser.id)
            .where(Project.id == project_id)
        )
        managers = (await session.scalars(
            select(TelegramUser.telegram_user_id)
            .join(ProjectMember, ProjectMember.user_id == TelegramUser.id)
            .where(ProjectMember.project_id == project_id,
                   ProjectMember.role.in_(("admin", "payment_manager")))
        )).all()
        ids = ([owner] if owner is not None else []) + list(managers)
        return order, tuple(dict.fromkeys(ids))


async def pending_manual_reviews(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID, page: int = 0,
) -> tuple[int, tuple[FileManualOrder, ...]]:
    """Never hide submitted receipts behind arbitrarily many pending invoices."""
    if not 0 <= page <= 1_000_000:
        raise ValueError("Недопустимая страница")
    async with system_project_session_scope(factory, project_id) as session:
        await require_payment_permission(
            session, project_id=project_id, actor_user_id=actor_id,
            permission=Permission.PAYMENT_VIEW,
        )
        predicates = (
            FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.status.in_(("pending", "submitted")),
        )
        total = await session.scalar(
            select(func.count(FileManualOrder.id)).where(*predicates)
        ) or 0
        rows = (await session.scalars(
            select(FileManualOrder).where(*predicates).order_by(
                # Receipts needing action first; otherwise newest to oldest.
                (FileManualOrder.status == "submitted").desc(),
                FileManualOrder.created_at.desc(),
                FileManualOrder.id.desc(),
            ).offset(page * 8).limit(8)
        )).all()
        return total, tuple(rows)


async def load_manual_review(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID, order_id: UUID,
) -> FileManualOrder:
    """Authorized direct lookup independent of review-list pagination."""
    async with system_project_session_scope(factory, project_id) as session:
        await require_payment_permission(
            session, project_id=project_id, actor_user_id=actor_id,
            permission=Permission.PAYMENT_VIEW,
        )
        order = await session.scalar(select(FileManualOrder).where(
            FileManualOrder.id == order_id,
            FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.status.in_(("pending", "submitted")),
        ))
        if order is None:
            raise ValueError("Заявка уже обработана или недоступна")
        return order


async def close_pending_manual_order(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID, order_id: UUID,
) -> FileManualOrder:
    """Close an abandoned unpaid invoice with an auditable reviewer identity.

    Only for pending orders with no submitted receipt. Admin should first
    check their bank to avoid cancelling a transfer not yet reported by buyer.
    """
    async with system_project_session_scope(factory, project_id) as session:
        await checkout_retirement_lock(session, project_id)
        await require_payment_permission(
            session, project_id=project_id, actor_user_id=actor_id,
            permission=Permission.PAYMENT_REJECT,
        )
        order = await session.scalar(select(FileManualOrder).where(
            FileManualOrder.id == order_id,
            FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
        ).with_for_update())
        if order is None:
            raise PermissionError("Счёт недоступен")
        if order.status != "pending" or order.receipt_file_id is not None:
            raise ValueError("Закрыть можно только счёт без отправленного чека")
        order.status = "cancelled"
        order.reviewed_by = actor_id
        order.reviewed_at = datetime.now(UTC)
        await session.flush()
        return order


async def review_manual_order(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
    order_id: UUID, approve: bool,
) -> tuple[FileManualOrder, bool]:
    async with system_project_session_scope(factory, project_id) as session:
        await require_payment_permission(
            session, project_id=project_id, actor_user_id=actor_id,
            permission=Permission.PAYMENT_APPROVE if approve else Permission.PAYMENT_REJECT,
        )
        order = await session.scalar(select(FileManualOrder).where(
            FileManualOrder.id == order_id, FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
        ).with_for_update())
        if order is None:
            raise ValueError("Заявка не найдена")
        target = "approved" if approve else "rejected"
        if order.status == target:
            return order, False
        if order.status != "submitted" or not order.receipt_file_id:
            raise ValueError("Проверять можно только заявку с чеком")
        order.status = target
        order.reviewed_at = datetime.now(UTC)
        order.reviewed_by = actor_id
        if approve:
            order.paid_at = order.reviewed_at
        return order, True


async def cancel_manual_order(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, order_id: UUID,
) -> None:
    async with system_project_session_scope(factory, project_id) as session:
        row = await session.scalar(select(FileManualOrder).where(
            FileManualOrder.id == order_id,
            FileManualOrder.project_id == project_id,
            FileManualOrder.telegram_bot_id == bot_id,
            FileManualOrder.telegram_user_id == buyer_id,
        ).with_for_update())
        if row is None:
            raise PermissionError("Это не ваша покупка")
        if row.status != "pending":
            raise ValueError("Отменить можно только неоплаченную заявку")
        row.status = "cancelled"
