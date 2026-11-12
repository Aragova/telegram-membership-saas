"""Project/bot-scoped Crypto Pay buyer choices, sharing existing storefronts.

Buyer checkout is deliberately gated until the 5% commercial sale ledger
accepts crypto evidence. No new menu, bot or payment-record table is created.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.crypto_pay.client import (
    CRYPTO,
    FIAT,
    CryptoPayError,
    invoice_price_spec,
)
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayApp, CryptoPayInvoice
from telegram_membership_saas.modules.file_sales.manual_models import FileManualPrice
from telegram_membership_saas.modules.file_sales.models import (
    FileAsset,
    FileFolder,
    FileProduct,
)
from telegram_membership_saas.modules.file_sales.payment_control_models import (
    FileSaleMethodVisibility,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import CurrencyAsset, PlanPrice
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource

CRYPTO_METHOD_ID = UUID(int=1)
CRYPTO_CURRENCIES = frozenset(FIAT | CRYPTO)


@dataclass(frozen=True, slots=True)
class BuyerCryptoInvoice:
    id: UUID
    url: str
    name: str
    kind: str
    amount: Decimal
    currency: str
    duration_value: int | None
    duration_unit: str | None


@dataclass(frozen=True, slots=True)
class CryptoFilePrice:
    currency: str
    amount: Decimal
    precision: int


async def crypto_merchant_ready(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
    require_buyer_enabled: bool = True,
) -> bool:
    """Only a mainnet app attached to the CURRENT project owner is saleable.

    Testnet is never advertised to real buyers. Historical retired app
    credentials still reconcile old invoices through the separate worker.
    """
    settings = get_settings()
    if settings.bot_token_encryption_key is None:
        return False
    if require_buyer_enabled and not settings.crypto_pay_buyer_enabled:
        return False
    found = await session.scalar(
        select(CryptoPayApp.id)
        .join(Project, Project.id == CryptoPayApp.project_id)
        .join(BotInstance, and_(
            BotInstance.project_id == CryptoPayApp.project_id,
            BotInstance.telegram_bot_id == CryptoPayApp.telegram_bot_id,
        ))
        .where(
            CryptoPayApp.project_id == project_id,
            CryptoPayApp.telegram_bot_id == bot_id,
            CryptoPayApp.owner_user_id == Project.owner_user_id,
            CryptoPayApp.environment == "mainnet",
            CryptoPayApp.status == "active",
            Project.status == "active",
            BotInstance.status == "registered",
        )
        .limit(1)
    )
    return found is not None


async def subscription_crypto_options(
    session: AsyncSession, *, project_id: UUID, bot_id: int,
):
    """Append one non-bank provider method to each truly live price.

    The checkout service revalidates the SAME PlanPrice before provider I/O.
    The synthetic method is display-only, never a PaymentMethod foreign key.
    """
    if not await crypto_merchant_ready(
        session, project_id=project_id, bot_id=bot_id,
    ):
        return ()

    # Lazy import protects Telegram commerce's existing cold-start graph.
    from telegram_membership_saas.modules.telegram_commerce.service import (
        CheckoutOption,
    )

    rows = (await session.execute(
        select(Plan, PlanPrice, CurrencyAsset.precision)
        .join(PlanPrice, and_(
            PlanPrice.project_id == Plan.project_id,
            PlanPrice.plan_id == Plan.id,
        ))
        .join(CurrencyAsset, CurrencyAsset.code == PlanPrice.currency_code)
        .join(PlanResource, and_(
            PlanResource.plan_id == Plan.id,
            PlanResource.project_id == Plan.project_id,
        ))
        .join(Resource, and_(
            Resource.id == PlanResource.resource_id,
            Resource.project_id == PlanResource.project_id,
        ))
        .where(
            Plan.project_id == project_id,
            Plan.status == "active",
            Plan.removed_at.is_(None),
            PlanPrice.status == "active",
            PlanPrice.currency_code.in_(CRYPTO_CURRENCIES),
            CurrencyAsset.status == "active",
            Resource.status == "active",
            Resource.removed_at.is_(None),
        )
        .distinct()
        .order_by(Plan.sort_order, Plan.name, PlanPrice.currency_code),
    )).all()
    choices = []
    for plan, price, precision in rows:
        try:
            invoice_price_spec(price.currency_code, price.amount)
        except (ValueError, CryptoPayError):
            continue
        choices.append(CheckoutOption(
            plan_id=plan.id, plan_name=plan.name,
            duration_value=plan.duration_value, duration_unit=plan.duration_unit,
            plan_price_id=price.id, currency_code=price.currency_code,
            amount=price.amount, currency_precision=precision,
            payment_method_id=CRYPTO_METHOD_ID,
            payment_method_name="💎 Crypto Pay",
        ))
    return tuple(choices)


async def file_crypto_prices(
    session: AsyncSession, *, project_id: UUID,
    bot_id: int, product_id: UUID,
) -> tuple[CryptoFilePrice, ...]:
    """Price rows are shared with fiat file prices, not manually named banks."""
    if not await crypto_merchant_ready(
        session, project_id=project_id, bot_id=bot_id,
    ):
        return ()
    # File owner switches are independent of the subscription chooser.
    blocked = await session.scalar(select(FileSaleMethodVisibility.enabled).where(
        FileSaleMethodVisibility.project_id == project_id,
        FileSaleMethodVisibility.telegram_bot_id == bot_id,
        FileSaleMethodVisibility.method_id == CRYPTO_METHOD_ID,
    ))
    if blocked is False:
        return ()
    rows = (await session.execute(
        select(FileManualPrice, CurrencyAsset.precision)
        .join(CurrencyAsset, CurrencyAsset.code == FileManualPrice.currency_code)
        .join(FileProduct, and_(
            FileProduct.id == FileManualPrice.product_id,
            FileProduct.project_id == FileManualPrice.project_id,
        ))
        .join(FileFolder, and_(
            FileFolder.id == FileProduct.folder_id,
            FileFolder.project_id == FileProduct.project_id,
        ))
        .where(
            FileManualPrice.project_id == project_id,
            FileManualPrice.product_id == product_id,
            FileProduct.telegram_bot_id == bot_id,
            FileProduct.status == "published",
            FileProduct.removed_at.is_(None),
            FileFolder.status == "active",
            FileFolder.removed_at.is_(None),
            FileManualPrice.currency_code.in_(CRYPTO_CURRENCIES),
            CurrencyAsset.status == "active",
            select(FileAsset.id).where(
                FileAsset.project_id == project_id,
                FileAsset.product_id == product_id,
            ).exists(),
        )
        .order_by(FileManualPrice.currency_code),
    )).all()
    choices = []
    for price, precision in rows:
        try:
            invoice_price_spec(price.currency_code, price.amount)
        except (ValueError, CryptoPayError):
            continue
        choices.append(CryptoFilePrice(
            currency=price.currency_code, amount=price.amount,
            precision=precision,
        ))
    return tuple(choices)


async def begin_buyer_crypto_checkout(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot_id: int, buyer_id: int, kind: str, target_id: UUID,
    currency: str | None = None, username: str | None = None,
    first_name: str | None = None,
    encryption_key: SecretStr | None = None,
):
    """Complete only real mainnet buyer invoices; do not use a fake paid flag.

    Every attempt rechecks the feature gate, current merchant binding,
    saleable target and buyer identity in the original project scope.
    """
    if encryption_key is None:
        raise ValueError("Crypto Pay is unavailable")
    async with system_project_session_scope(factory, project_id) as session:
        if not await crypto_merchant_ready(
            session, project_id=project_id, bot_id=bot_id,
        ):
            raise PermissionError("Crypto Pay is unavailable for this bot")
        if kind == "subscription":
            currency = await session.scalar(select(PlanPrice.currency_code).where(
                PlanPrice.project_id == project_id,
                PlanPrice.id == target_id,
            ))
        elif kind != "file":
            raise ValueError("Invalid Crypto Pay target")
        if currency not in CRYPTO_CURRENCIES:
            raise ValueError("Crypto Pay currency is unavailable")

    # Import only at ingress to avoid cycles with file_sales.service.
    from telegram_membership_saas.modules.crypto_pay.checkout import (
        issue_reserved_crypto_invoice,
        reserve_crypto_invoice,
    )

    local_id = await reserve_crypto_invoice(
        factory, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id,
        kind=kind, target_id=target_id, currency=currency,
        username=username, first_name=first_name,
        reuse_recent=True, require_mainnet=True,
    )
    issued = await issue_reserved_crypto_invoice(
        factory, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id,
        invoice_id=local_id, encryption_key=encryption_key,
        require_mainnet=True,
    )
    async with system_project_session_scope(factory, project_id) as session:
        original = await session.scalar(select(CryptoPayInvoice).where(
            CryptoPayInvoice.id == issued.id,
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.telegram_bot_id == bot_id,
            CryptoPayInvoice.telegram_user_id == buyer_id,
        ))
        if original is None or original.invoice_url != issued.url:
            raise CryptoPayError("The original Crypto Pay invoice changed")
        return BuyerCryptoInvoice(
            id=issued.id, url=issued.url,
            name=original.name_snapshot, kind=original.kind,
            amount=original.amount, currency=original.currency_code,
            duration_value=original.duration_value,
            duration_unit=original.duration_unit,
        )
