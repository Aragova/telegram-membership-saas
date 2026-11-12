"""Internal Crypto Pay checkout reservations and authoritative invoice reads.

NO BUYER ROUTE calls this module yet. In particular, provider status alone
must never set CryptoPayInvoice.status='paid' or grant any digital goods:
that requires the separate atomic fulfillment transaction and recovery worker.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.crypto_pay.client import (
    CryptoPayClient,
    CryptoPayError,
    CryptoPayInvoiceReceipt,
    invoice_price_spec,
    verify_created_invoice_snapshot,
    verify_paid_invoice,
)
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayApp, CryptoPayInvoice
from telegram_membership_saas.modules.crypto_pay.service import load_crypto_app_token
from telegram_membership_saas.modules.file_sales.manual_models import FileManualPrice
from telegram_membership_saas.modules.file_sales.models import FileAsset, FileFolder, FileProduct
from telegram_membership_saas.modules.file_sales.service import _ready
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.payments.request_service import get_or_create_project_customer
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import CurrencyAsset, PlanPrice
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock


@dataclass(frozen=True, slots=True)
class IssuedCryptoInvoice:
    id: UUID
    url: str
    amount: Decimal
    currency_code: str


async def reserve_crypto_invoice(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, kind: str,
    target_id: UUID, currency: str, username: str | None = None,
    first_name: str | None = None,
) -> UUID:
    """Freeze a currently saleable file/plan in its seller's active app.

    Only trusted connected-bot ingress must supply project/bot/buyer identity.
    The reservation is committed before any external provider call.
    """
    if kind not in ("file", "subscription"):
        raise ValueError("Unsupported Crypto Pay checkout kind")
    currency = currency.upper()
    async with system_project_session_scope(factory, project_id) as session:
        await checkout_retirement_lock(session, project_id)
        await _ready(session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id)
        app = await session.scalar(select(CryptoPayApp).join(
            Project, Project.id == CryptoPayApp.project_id,
        ).where(
            CryptoPayApp.project_id == project_id,
            CryptoPayApp.telegram_bot_id == bot_id,
            CryptoPayApp.owner_user_id == Project.owner_user_id,
            CryptoPayApp.status == "active",
        ).with_for_update(of=CryptoPayApp))
        if app is None:
            raise ValueError("The seller has not enabled Crypto Pay")

        pending = await session.scalar(select(func.count(CryptoPayInvoice.id)).where(
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.telegram_bot_id == bot_id,
            CryptoPayInvoice.telegram_user_id == buyer_id,
            or_(
                CryptoPayInvoice.status == "issuing",
                and_(
                    CryptoPayInvoice.status.in_(("reserved", "issued")),
                    CryptoPayInvoice.created_at >= datetime.now(UTC) - timedelta(days=1),
                ),
            ),
        )) or 0
        if pending >= 10:
            raise ValueError("Too many pending Crypto Pay invoices")

        customer_id = None
        plan_id = None
        plan_price_id = None
        product_id = None
        duration_value = None
        duration_unit = None
        if kind == "subscription":
            row = (await session.execute(
                select(Plan, PlanPrice).join(
                    PlanPrice,
                    (PlanPrice.project_id == Plan.project_id)
                    & (PlanPrice.plan_id == Plan.id),
                ).join(
                    CurrencyAsset, CurrencyAsset.code == PlanPrice.currency_code,
                ).where(
                    Plan.project_id == project_id,
                    PlanPrice.id == target_id,
                    PlanPrice.currency_code == currency,
                    Plan.status == "active",
                    Plan.removed_at.is_(None),
                    PlanPrice.status == "active",
                    CurrencyAsset.status == "active",
                ).with_for_update(of=Plan),
            )).first()
            if row is None:
                raise ValueError("Subscription price is no longer available")
            plan, price = row
            linked = await session.scalar(select(Resource.id).join(
                PlanResource,
                (PlanResource.project_id == Resource.project_id)
                & (PlanResource.resource_id == Resource.id),
            ).where(
                PlanResource.project_id == project_id,
                PlanResource.plan_id == plan.id,
                Resource.status == "active",
                Resource.removed_at.is_(None),
            ).limit(1))
            if linked is None:
                raise ValueError("Subscription has no active resource")
            # Import lazily: customer runtime previously had a cold-start
            # import cycle via telegram_commerce.__init__ and Stars handlers.
            from telegram_membership_saas.modules.telegram_commerce.service import (
                upsert_telegram_user,
            )

            user = await upsert_telegram_user(
                session, telegram_user_id=buyer_id,
                username=username, first_name=first_name,
            )
            customer = await get_or_create_project_customer(
                session, project_id=project_id, user_id=user.id,
            )
            customer = await session.scalar(select(ProjectCustomer).where(
                ProjectCustomer.id == customer.id,
                ProjectCustomer.project_id == project_id,
            ).with_for_update())
            if customer is None or customer.is_banned:
                raise PermissionError("Purchases are disabled for this buyer")
            customer_id = customer.id
            plan_id, plan_price_id = plan.id, price.id
            duration_value, duration_unit = plan.duration_value, plan.duration_unit
            name, amount = plan.name, price.amount
        else:
            row = (await session.execute(
                select(FileProduct, FileManualPrice).join(
                    FileManualPrice,
                    (FileManualPrice.product_id == FileProduct.id)
                    & (FileManualPrice.project_id == FileProduct.project_id),
                ).join(
                    FileFolder,
                    (FileFolder.id == FileProduct.folder_id)
                    & (FileFolder.project_id == FileProduct.project_id),
                ).join(
                    CurrencyAsset, CurrencyAsset.code == FileManualPrice.currency_code,
                ).where(
                    FileProduct.project_id == project_id,
                    FileProduct.telegram_bot_id == bot_id,
                    FileProduct.id == target_id,
                    FileProduct.status == "published",
                    FileProduct.removed_at.is_(None),
                    FileFolder.status == "active",
                    FileFolder.removed_at.is_(None),
                    FileManualPrice.currency_code == currency,
                    CurrencyAsset.status == "active",
                ).with_for_update(of=FileProduct),
            )).first()
            if row is None:
                raise ValueError("File or price is no longer available")
            product, price = row
            has_asset = await session.scalar(select(FileAsset.id).where(
                FileAsset.project_id == project_id,
                FileAsset.product_id == product.id,
            ).limit(1))
            if has_asset is None:
                raise ValueError("This file is not ready for delivery")
            product_id = product.id
            name, amount = product.name, price.amount

        spec = invoice_price_spec(currency, amount)
        order_id = uuid4()
        record = CryptoPayInvoice(
            id=order_id, project_id=project_id, app_id=app.id,
            telegram_bot_id=bot_id, telegram_user_id=buyer_id,
            customer_id=customer_id, kind=kind, plan_id=plan_id,
            plan_price_id=plan_price_id, product_id=product_id,
            name_snapshot=name, duration_value=duration_value,
            duration_unit=duration_unit, amount=amount,
            currency_code=currency, currency_type=spec["currency_type"],
            payload="cp:" + order_id.hex, status="reserved",
        )
        session.add(record)
        await session.flush()
        return order_id


async def issue_reserved_crypto_invoice(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, buyer_id: int, invoice_id: UUID,
    encryption_key: SecretStr | None,
    client_factory: Callable[..., CryptoPayClient] = CryptoPayClient,
) -> IssuedCryptoInvoice:
    """Commit 'issuing' BEFORE contacting Crypto Pay to prevent duplicate bills.

    A network timeout, process crash or failed DB finalization leaves 'issuing'
    and requires explicit provider reconciliation, NEVER blind createInvoice
    retry. No buyer-facing route or automatic paid delivery exists yet.
    """
    async with system_project_session_scope(factory, project_id) as session:
        await checkout_retirement_lock(session, project_id)
        record = await session.scalar(select(CryptoPayInvoice).where(
            CryptoPayInvoice.id == invoice_id,
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.telegram_bot_id == bot_id,
            CryptoPayInvoice.telegram_user_id == buyer_id,
        ).with_for_update())
        if record is None:
            raise PermissionError("Invoice does not belong to this buyer and bot")
        if record.status == "issued" and record.invoice_url:
            return IssuedCryptoInvoice(
                id=record.id, url=record.invoice_url,
                amount=record.amount, currency_code=record.currency_code,
            )
        if record.status == "issuing":
            raise CryptoPayError("Invoice issuance is awaiting provider reconciliation")
        if record.status != "reserved":
            raise ValueError("Invoice cannot be issued in its current state")
        await _ready(
            session, project_id=project_id, bot_id=bot_id, buyer_id=buyer_id,
        )
        if record.created_at < datetime.now(UTC) - timedelta(minutes=15):
            raise ValueError("Crypto Pay checkout reservation has expired")

        # A product or channel can disappear between reservation and issue.
        # Do not create a payable invoice for an item that cannot be delivered.
        # Existing issued invoices stay historically payable/recoverable.
        if record.kind == "file":
            available = await session.scalar(select(FileProduct.id).join(
                FileFolder,
                (FileFolder.id == FileProduct.folder_id)
                & (FileFolder.project_id == FileProduct.project_id),
            ).where(
                FileProduct.id == record.product_id,
                FileProduct.project_id == project_id,
                FileProduct.telegram_bot_id == bot_id,
                FileProduct.status == "published",
                FileProduct.removed_at.is_(None),
                FileFolder.status == "active",
                FileFolder.removed_at.is_(None),
            ).with_for_update(of=FileProduct))
            asset = await session.scalar(select(FileAsset.id).where(
                FileAsset.project_id == project_id,
                FileAsset.product_id == record.product_id,
            ).limit(1))
            if available is None or asset is None:
                raise ValueError("Reserved file has been removed or is unavailable")
        elif record.kind == "subscription":
            available = await session.scalar(select(Plan.id).join(
                PlanPrice,
                (PlanPrice.project_id == Plan.project_id)
                & (PlanPrice.plan_id == Plan.id),
            ).where(
                Plan.id == record.plan_id,
                Plan.project_id == project_id,
                PlanPrice.id == record.plan_price_id,
                Plan.status == "active",
                Plan.removed_at.is_(None),
            ).with_for_update(of=Plan))
            resource = await session.scalar(select(Resource.id).join(
                PlanResource,
                (PlanResource.project_id == Resource.project_id)
                & (PlanResource.resource_id == Resource.id),
            ).where(
                PlanResource.project_id == project_id,
                PlanResource.plan_id == record.plan_id,
                Resource.status == "active",
                Resource.removed_at.is_(None),
            ).limit(1))
            if available is None or resource is None:
                raise ValueError("Reserved subscription is no longer available")
        else:
            raise ValueError("Unsupported Crypto Pay checkout kind")

        app, token = await load_crypto_app_token(
            session, project_id=project_id, app_id=record.app_id,
            encryption_key=encryption_key,
        )
        owner = await session.scalar(select(Project.owner_user_id).where(
            Project.id == project_id, Project.status == "active",
        ))
        if (app.status != "active" or app.telegram_bot_id != bot_id
                or app.owner_user_id != owner):
            raise PermissionError("Merchant connection was replaced or disabled")
        testnet = app.environment == "testnet"
        amount, currency, payload = record.amount, record.currency_code, record.payload
        description = record.name_snapshot
        record.status = "issuing"
        await session.flush()
    # The transition above is durable before provider I/O. Never return a
    # link or retry an uncertain request until a separate verified recovery.
    async with client_factory(token, testnet=testnet) as provider:
        response = await provider.create_invoice(
            currency=currency, amount=amount,
            payload=payload, description=description,
        )
    async with system_project_session_scope(factory, project_id) as session:
        await checkout_retirement_lock(session, project_id)
        record = await session.scalar(select(CryptoPayInvoice).where(
            CryptoPayInvoice.id == invoice_id,
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.app_id == app.id,
            CryptoPayInvoice.telegram_bot_id == bot_id,
            CryptoPayInvoice.telegram_user_id == buyer_id,
        ).with_for_update())
        if record is None or record.status != "issuing":
            raise CryptoPayError("Crypto Pay issuance changed before finalization")
        record.provider_invoice_id = response["invoice_id"]
        record.invoice_url = response["bot_invoice_url"]
        record.status = "issued"
        await session.flush()
        return IssuedCryptoInvoice(
            id=record.id, url=record.invoice_url,
            amount=record.amount, currency_code=record.currency_code,
        )



async def recover_uncertain_crypto_invoice(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, invoice_id: UUID, encryption_key: SecretStr | None,
    client_factory: Callable[..., CryptoPayClient] = CryptoPayClient,
    max_pages: int = 5,
) -> IssuedCryptoInvoice:
    """Find a timed-out createInvoice by its unique frozen payload.

    Scans up to 500 recent invoices on the original app. Absence in a
    bounded page scan is NEVER proof the provider did not create the invoice:
    leave the durable 'issuing' row for later/manual recovery.
    """
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise ValueError("Crypto Pay reconciliation page limit is invalid")
    async with system_project_session_scope(factory, project_id) as session:
        record = await session.scalar(select(CryptoPayInvoice).where(
            CryptoPayInvoice.id == invoice_id,
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.status == "issuing",
        ))
        if record is None:
            raise CryptoPayError("Crypto Pay has no uncertain invoice to reconcile")
        app, token = await load_crypto_app_token(
            session, project_id=project_id, app_id=record.app_id,
            encryption_key=encryption_key,
        )
        payload = record.payload
        amount, code, currency_type = (
            record.amount, record.currency_code, record.currency_type,
        )
        testnet = app.environment == "testnet"
        app_id = app.id
    found: list[dict] = []
    async with client_factory(token, testnet=testnet) as provider:
        for page in range(max_pages):
            invoices = await provider.list_recent_invoices(
                offset=page * 100, count=100,
            )
            found.extend(invoice for invoice in invoices if invoice.get("payload") == payload)
            if len(invoices) < 100:
                break
    if len(found) != 1:
        raise CryptoPayError("Uncertain Crypto Pay invoice requires further reconciliation")
    provider_id, link = verify_created_invoice_snapshot(
        found[0], payload=payload, amount=amount,
        currency_code=code, currency_type=currency_type, testnet=testnet,
    )
    async with system_project_session_scope(factory, project_id) as session:
        record = await session.scalar(select(CryptoPayInvoice).where(
            CryptoPayInvoice.id == invoice_id,
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.app_id == app_id,
        ).with_for_update())
        if (record is None or record.status != "issuing" or record.payload != payload
                or record.amount != amount or record.currency_code != code):
            raise CryptoPayError("Invoice changed during provider reconciliation")
        record.provider_invoice_id = provider_id
        record.invoice_url = link
        record.status = "issued"
        await session.flush()
        return IssuedCryptoInvoice(
            id=invoice_id, url=link, amount=amount, currency_code=code,
        )


async def inspect_provider_paid_invoice(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, invoice_id: UUID,
    encryption_key: SecretStr | None,
    client_factory: Callable[..., CryptoPayClient] = CryptoPayClient,
) -> CryptoPayInvoiceReceipt:
    """Read-only verified provider evidence from the ORIGINAL merchant app.

    This intentionally does not set paid, create Payment/Subscription or
    deliver a file. Those changes must share one durable fulfillment commit.
    """
    async with system_project_session_scope(factory, project_id) as session:
        record = await session.scalar(select(CryptoPayInvoice).where(
            CryptoPayInvoice.id == invoice_id,
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.status == "issued",
        ))
        if record is None or record.provider_invoice_id is None:
            raise CryptoPayError("No issued Crypto Pay invoice is available")
        app, token = await load_crypto_app_token(
            session, project_id=project_id, app_id=record.app_id,
            encryption_key=encryption_key,
        )
        if app.telegram_bot_id != record.telegram_bot_id:
            raise CryptoPayError("Merchant app and buyer bot do not match")
        async with client_factory(
            token, testnet=app.environment == "testnet",
        ) as provider:
            receipts = await provider.get_invoices((record.provider_invoice_id,))
        matching = [r for r in receipts if r["invoice_id"] == record.provider_invoice_id]
        if len(matching) != 1:
            raise CryptoPayError("Crypto Pay invoice confirmation is missing")
        return verify_paid_invoice(
            matching[0], invoice_id=record.provider_invoice_id,
            payload=record.payload, amount=record.amount,
            currency_code=record.currency_code,
            currency_type=record.currency_type,
        )
