"""Atomic Crypto Pay settlement into existing subscription and file sale ledgers.

The ONLY public entrypoint checks the exact provider invoice on the original
merchant credential first. No buyer supplied message, link, webhook flag or
unsigned callback can call _apply_verified_receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.access.service import reconcile_access_target
from telegram_membership_saas.modules.crypto_pay.checkout import inspect_provider_paid_invoice
from telegram_membership_saas.modules.crypto_pay.client import (
    CryptoPayClient,
    CryptoPayError,
    CryptoPayInvoiceReceipt,
)
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayApp, CryptoPayInvoice
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.file_sales.models import FileAsset
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    OutboxEventStatus,
    OutboxEventType,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.stars_subscription_timeline import (
    historical_stars_start,
    load_refunded_star_sources,
    load_surviving_crypto_sources,
    load_surviving_paid_sources,
    owner_end_times,
    rebuild_paid_windows_after,
    refresh_paid_subscription_summary,
)
from telegram_membership_saas.modules.subscriptions import (
    Subscription,
    SubscriptionStatus,
    calculate_period_end,
)
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock


@dataclass(frozen=True, slots=True)
class CryptoSettlement:
    invoice_id: UUID
    kind: str
    changed: bool
    paid_at: datetime
    buyer_id: int


async def _apply_verified_receipt(
    session: AsyncSession, *, project_id: UUID, invoice_id: UUID,
    receipt: CryptoPayInvoiceReceipt,
) -> CryptoSettlement:
    """Call ONLY with a verified response from getInvoices on original app.

    All DB state and access effects commit in the surrounding project
    transaction. Project lock serializes manual, Stars and Crypto renewals.
    """
    await checkout_retirement_lock(session, project_id)
    invoice = await session.scalar(select(CryptoPayInvoice).where(
        CryptoPayInvoice.project_id == project_id,
        CryptoPayInvoice.id == invoice_id,
    ).with_for_update())
    if invoice is None or invoice.provider_invoice_id is None:
        raise CryptoPayError("Crypto Pay invoice is not accessible")
    if (
        receipt.invoice_id != invoice.provider_invoice_id
        or receipt.payload != invoice.payload
        or receipt.amount != invoice.amount
        or receipt.currency_code != invoice.currency_code
        or receipt.currency_type != invoice.currency_type
        or receipt.paid_at.tzinfo is None
        or receipt.paid_at > datetime.now(UTC) + timedelta(minutes=5)
    ):
        raise CryptoPayError("Verified Crypto Pay receipt mismatches frozen invoice")
    app = await session.scalar(select(CryptoPayApp).where(
        CryptoPayApp.id == invoice.app_id,
        CryptoPayApp.project_id == project_id,
        CryptoPayApp.telegram_bot_id == invoice.telegram_bot_id,
    ))
    if app is None:
        raise CryptoPayError("Original Crypto Pay merchant app is missing")
    if invoice.status == "paid":
        if invoice.paid_at != receipt.paid_at:
            raise CryptoPayError("Already-paid invoice has conflicting timestamp")
        return CryptoSettlement(
            invoice_id=invoice.id, kind=invoice.kind, changed=False,
            paid_at=invoice.paid_at, buyer_id=invoice.telegram_user_id,
        )
    if invoice.status != "issued":
        raise CryptoPayError("Crypto Pay invoice is not issued or is terminal")

    moment = receipt.paid_at.astimezone(UTC)
    if invoice.kind == "file":
        # Preserve immutable sale target even after a storefront tombstone.
        item = await session.scalar(select(FileAsset.id).where(
            FileAsset.project_id == project_id,
            FileAsset.product_id == invoice.product_id,
        ).limit(1))
        if item is None:
            raise RuntimeError("Paid file invoice has no original assets")
        invoice.status = "paid"
        invoice.paid_at = moment
        await session.flush()
        # File push/delivery worker will use this invoice as the single
        # paid order ID; the buyer's My Files already reads the same row.
        return CryptoSettlement(
            invoice_id=invoice.id, kind=invoice.kind, changed=True,
            paid_at=moment, buyer_id=invoice.telegram_user_id,
        )

    if invoice.kind != "subscription" or invoice.customer_id is None:
        raise CryptoPayError("Crypto Pay subscription target is invalid")

    customer = await session.scalar(select(ProjectCustomer).where(
        ProjectCustomer.id == invoice.customer_id,
        ProjectCustomer.project_id == project_id,
    ).with_for_update())
    if customer is None:
        raise RuntimeError("Paid Crypto Pay buyer no longer exists")
    subscription = await session.scalar(select(Subscription).where(
        Subscription.project_id == project_id,
        Subscription.customer_id == invoice.customer_id,
        Subscription.plan_id == invoice.plan_id,
    ).with_for_update())
    project = await session.scalar(select(Project).where(
        Project.id == project_id,
    ))
    if project is None:
        raise RuntimeError("Paid Crypto Pay project no longer exists")

    # Capture exactly the resources sold, not the owner's latest plan edit.
    if not isinstance(invoice.resource_ids_snapshot, list):
        raise CryptoPayError("Frozen subscription resources are unavailable")
    try:
        resource_ids = tuple(UUID(value) for value in invoice.resource_ids_snapshot)
    except (TypeError, ValueError, AttributeError) as exc:
        raise CryptoPayError("Frozen subscription resources are malformed") from exc
    if not resource_ids or len(resource_ids) != len(set(resource_ids)):
        raise CryptoPayError("Frozen subscription resources are missing or duplicated")
    existing_resources = set((await session.scalars(select(Resource.id).where(
        Resource.project_id == project_id,
        Resource.id.in_(resource_ids),
    ))).all())
    if existing_resources != set(resource_ids):
        raise RuntimeError("Paid Crypto Pay subscription resources were removed")

    terminations = await owner_end_times(
        session, project_id=project_id, subscription=subscription,
    )
    # Follow existing Stars semantics: a payment timestamped before explicit
    # owner termination remains financial evidence, not a new access grant.
    if any(moment <= ended for ended in terminations):
        invoice.status = "paid"
        invoice.paid_at = moment
        await session.flush()
        return CryptoSettlement(
            invoice_id=invoice.id, kind=invoice.kind, changed=True,
            paid_at=moment, buyer_id=invoice.telegram_user_id,
        )

    manual, stars = await load_surviving_paid_sources(
        session, project_id=project_id, customer_id=customer.id,
        plan_id=invoice.plan_id,
    )
    crypto = await load_surviving_crypto_sources(
        session, project_id=project_id, customer_id=customer.id,
        plan_id=invoice.plan_id,
    )
    refunded = await load_refunded_star_sources(
        session, project_id=project_id, customer_id=customer.id,
        plan_id=invoice.plan_id,
    )
    start_at = historical_stars_start(
        moment, manual, stars, refunded, terminations,
        project.timezone, crypto=crypto,
    )
    end_at = calculate_period_end(
        start_at, timezone_name=project.timezone,
        duration_value=invoice.duration_value,
        duration_unit=invoice.duration_unit,
    )
    invoice.status = "paid"
    invoice.paid_at = moment
    if subscription is None:
        subscription = Subscription(
            project_id=project_id, customer_id=customer.id,
            plan_id=invoice.plan_id,
            status=SubscriptionStatus.ACTIVE.value,
            started_at=start_at, current_period_start=start_at,
            current_period_end=end_at, last_payment_id=None,
            last_star_order_id=None, last_crypto_invoice_id=invoice.id,
        )
        session.add(subscription)
    else:
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.started_at = min(subscription.started_at, start_at)
        subscription.current_period_start = start_at
        subscription.current_period_end = end_at
        subscription.last_payment_id = None
        subscription.last_star_order_id = None
        subscription.last_crypto_invoice_id = invoice.id
    grants = [
        EntitlementGrant(
            project_id=project_id, customer_id=customer.id,
            resource_id=resource_id,
            source=EntitlementSource.CRYPTO_PAYMENT.value,
            source_id=invoice.id,
            status=EntitlementGrantStatus.ACTIVE.value,
            valid_from=start_at, valid_until=end_at,
        )
        for resource_id in resource_ids
    ]
    session.add_all(grants)
    await session.flush()
    repaired = await rebuild_paid_windows_after(
        session, project_id=project_id, customer_id=customer.id,
        plan_id=invoice.plan_id, after=moment,
        termination_times=terminations, include_equal=True,
    )
    await refresh_paid_subscription_summary(
        session, project_id=project_id, customer_id=customer.id,
        plan_id=invoice.plan_id, subscription=subscription,
        as_of=datetime.now(UTC),
    )
    for resource_id in sorted(repaired):
        await reconcile_access_target(
            session, project_id=project_id,
            customer_id=customer.id, resource_id=resource_id,
        )
    session.add_all([
        OutboxEvent(
            project_id=project_id,
            event_type=OutboxEventType.ACCESS_RECALCULATE.value,
            aggregate_type="entitlement_grant",
            aggregate_id=grant.id,
            dedupe_key=f"entitlement_grant:{grant.id}:access_recalculate",
            payload={
                "customer_id": str(customer.id),
                "resource_id": str(grant.resource_id),
                "grant_id": str(grant.id),
            },
            status=OutboxEventStatus.PENDING.value,
        )
        for grant in grants
    ])
    await session.flush()
    return CryptoSettlement(
        invoice_id=invoice.id, kind=invoice.kind, changed=True,
        paid_at=moment, buyer_id=invoice.telegram_user_id,
    )


async def settle_crypto_pay_invoice(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    invoice_id: UUID, encryption_key: SecretStr | None,
    client_factory: type[CryptoPayClient] = CryptoPayClient,
) -> CryptoSettlement:
    """Trusted recovery/ingress only; never accept a buyer-supplied paid flag.

    First check the issuing app on Crypto Pay with getInvoices. Then verify
    the same immutable original invoice again under a project lock. Replays
    cannot produce duplicate file purchases or entitlement grants.
    """
    async with system_project_session_scope(factory, project_id) as session:
        existing = await session.scalar(select(CryptoPayInvoice).where(
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.id == invoice_id,
        ))
        if existing is None or existing.provider_invoice_id is None:
            raise CryptoPayError("Crypto Pay invoice does not exist")
        if existing.status == "paid":
            if existing.paid_at is None:
                raise RuntimeError("Crypto Pay paid state lacks timestamp")
            return CryptoSettlement(
                invoice_id=existing.id, kind=existing.kind, changed=False,
                paid_at=existing.paid_at, buyer_id=existing.telegram_user_id,
            )
        if existing.status != "issued":
            raise CryptoPayError("Crypto Pay invoice has no payable state")

    receipt = await inspect_provider_paid_invoice(
        factory, project_id=project_id, invoice_id=invoice_id,
        encryption_key=encryption_key, client_factory=client_factory,
    )
    async with system_project_session_scope(factory, project_id) as session:
        return await _apply_verified_receipt(
            session, project_id=project_id, invoice_id=invoice_id,
            receipt=receipt,
        )
