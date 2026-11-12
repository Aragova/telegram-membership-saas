"""PostgreSQL/RLS-backed Crypto Pay paid file and subscription settlement.

No buyer-facing Crypto Pay handler exists. The only settlement API reads
authoritative getInvoices on the app that originally issued the invoice.
"""
import os
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_crypto_pay_invoice_reservations import Provider, _fixture
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.crypto_pay.checkout import (
    issue_reserved_crypto_invoice,
    reserve_crypto_invoice,
)
from telegram_membership_saas.modules.crypto_pay.client import CryptoPayError
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayInvoice
from telegram_membership_saas.modules.crypto_pay.settlement import settle_crypto_pay_invoice
from telegram_membership_saas.modules.entitlements import EntitlementGrant
from telegram_membership_saas.modules.file_sales.service import (
    list_purchases_page,
    mark_file_delivery,
    purchased_assets,
)
from telegram_membership_saas.modules.outbox import OutboxEvent
from telegram_membership_saas.modules.owner_setup.channel_management import (
    remove_channel_without_paid_purchases,
)
from telegram_membership_saas.modules.owner_setup.obligations import (
    end_owner_subscription,
    revoke_owner_grant,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.subscriptions import Subscription

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Real migrated non-bypass PostgreSQL/RLS required",
)


def _reset_provider() -> None:
    Provider.created = []
    Provider.invoice_data = {}


async def _shop(factory, key):
    owner, _, project, bot_uuid, _ = await _project(factory, _tid())
    async with system_project_session_scope(factory, project) as session:
        bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
            BotInstance.id == bot_uuid,
        ))
    _, product_id = await _fixture(
        factory, owner_id=owner, project_id=project, bot_id=bot_id, key=key,
    )
    return owner, project, bot_id, product_id


async def _issue(factory, *, project, bot_id, buyer, kind, target_id, key):
    local_id = await reserve_crypto_invoice(
        factory, project_id=project, bot_id=bot_id, buyer_id=buyer,
        kind=kind, target_id=target_id, currency="RUB",
    )
    issued = await issue_reserved_crypto_invoice(
        factory, project_id=project, bot_id=bot_id, buyer_id=buyer,
        invoice_id=local_id, encryption_key=key, client_factory=Provider,
    )
    assert issued.id == local_id
    return local_id


async def test_verified_file_payment_is_one_paid_record_in_myfiles():
    _reset_provider()
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product_id = await _shop(factory, key)
        buyer = _tid()
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer,
            kind="file", target_id=product_id, key=key,
        )
        first = await settle_crypto_pay_invoice(
            factory, project_id=project, invoice_id=invoice_id,
            encryption_key=key, client_factory=Provider,
        )
        second = await settle_crypto_pay_invoice(
            factory, project_id=project, invoice_id=invoice_id,
            encryption_key=key, client_factory=Provider,
        )
        assert first.kind == "file" and first.changed
        assert second.kind == "file" and not second.changed
        assert first.invoice_id == second.invoice_id == invoice_id
        total, files = await list_purchases_page(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer,
        )
        assert total == 1 and files[0].id == invoice_id
        order, assets = await purchased_assets(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer, purchase_id=invoice_id,
        )
        assert order.product_name == "File"
        assert len(assets) == 1
        await mark_file_delivery(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer, purchase_id=invoice_id,
        )
        async with system_project_session_scope(factory, project) as session:
            invoice = await session.get(CryptoPayInvoice, invoice_id)
            assert invoice.status == "paid"
            assert invoice.paid_at is not None
            assert invoice.last_delivered_at is not None
            assert not (await session.scalars(select(EntitlementGrant).where(
                EntitlementGrant.project_id == project,
            ))).all()
        with pytest.raises(PermissionError):
            await purchased_assets(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=_tid(), purchase_id=invoice_id,
            )
    finally:
        await engine.dispose()


async def test_forged_crypto_receipt_never_marks_paid_or_releases_file():
    _reset_provider()
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product_id = await _shop(factory, key)
        buyer = _tid()
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer,
            kind="file", target_id=product_id, key=key,
        )
        provider_id = next(iter(Provider.invoice_data))
        Provider.invoice_data[provider_id]["amount"] = "999"
        with pytest.raises(CryptoPayError):
            await settle_crypto_pay_invoice(
                factory, project_id=project, invoice_id=invoice_id,
                encryption_key=key, client_factory=Provider,
            )
        async with system_project_session_scope(factory, project) as session:
            invoice = await session.get(CryptoPayInvoice, invoice_id)
            assert invoice.status == "issued"
            assert invoice.paid_at is None
        with pytest.raises(PermissionError):
            await purchased_assets(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer, purchase_id=invoice_id,
            )
    finally:
        await engine.dispose()


async def test_crypto_subscription_grants_original_resources_and_renews_once():
    _reset_provider()
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, _ = await _shop(factory, key)
        async with system_project_session_scope(factory, project) as session:
            resource = await session.scalar(select(Resource).where(
                Resource.project_id == project,
            ))
            plan = Plan(
                project_id=project, name="Monthly crypto",
                duration_value=1, duration_unit="month", status="active",
            )
            session.add(plan)
            await session.flush()
            price = PlanPrice(
                project_id=project, plan_id=plan.id,
                currency_code="RUB", version=1,
                amount=Decimal("299"), status="active",
            )
            session.add_all([
                price, PlanResource(
                    project_id=project, plan_id=plan.id, resource_id=resource.id,
                ),
            ])
            await session.flush()
            price_id, resource_id = price.id, resource.id

        buyer = _tid()
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer,
            kind="subscription", target_id=price_id, key=key,
        )
        # Edit the plan after payment issuance; fulfil exactly what was sold.
        async with system_project_session_scope(factory, project) as session:
            link = await session.scalar(select(PlanResource).where(
                PlanResource.project_id == project,
                PlanResource.plan_id == plan.id,
            ))
            await session.delete(link)

        first = await settle_crypto_pay_invoice(
            factory, project_id=project, invoice_id=invoice_id,
            encryption_key=key, client_factory=Provider,
        )
        second = await settle_crypto_pay_invoice(
            factory, project_id=project, invoice_id=invoice_id,
            encryption_key=key, client_factory=Provider,
        )
        assert first.kind == "subscription" and first.changed
        assert second.kind == "subscription" and not second.changed
        async with system_project_session_scope(factory, project) as session:
            invoice = await session.get(CryptoPayInvoice, invoice_id)
            subscriptions = (await session.scalars(select(Subscription).where(
                Subscription.project_id == project,
            ))).all()
            grants = (await session.scalars(select(EntitlementGrant).where(
                EntitlementGrant.project_id == project,
                EntitlementGrant.source_id == invoice_id,
            ))).all()
            outbox = (await session.scalars(select(OutboxEvent).where(
                OutboxEvent.project_id == project,
            ))).all()
            assert invoice.status == "paid"
            assert invoice.resource_ids_snapshot == [str(resource_id)]
            assert len(subscriptions) == 1
            assert subscriptions[0].last_crypto_invoice_id == invoice_id
            assert len(grants) == 1
            assert grants[0].resource_id == resource_id
            assert grants[0].source == "crypto_payment"
            assert len(outbox) == 1
            assert outbox[0].event_type == "access_recalculate"
    finally:
        await engine.dispose()



async def test_owner_termination_revokes_crypto_paid_grants_and_access():
    """A lifetime or finite crypto grant must not survive owner termination."""
    _reset_provider()
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        owner, project, bot_id, _ = await _shop(factory, key)
        async with system_project_session_scope(factory, project) as session:
            resource_id = await session.scalar(select(Resource.id).where(
                Resource.project_id == project,
            ))
            plan = Plan(
                project_id=project, name="Lifetime crypto",
                duration_value=None, duration_unit="lifetime", status="active",
            )
            session.add(plan)
            await session.flush()
            price = PlanPrice(
                project_id=project, plan_id=plan.id,
                currency_code="RUB", version=1,
                amount=Decimal("1000"), status="active",
            )
            session.add_all([
                price, PlanResource(
                    project_id=project, plan_id=plan.id, resource_id=resource_id,
                ),
            ])
            await session.flush()
            price_id = price.id
        buyer = _tid()
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer,
            kind="subscription", target_id=price_id, key=key,
        )
        await settle_crypto_pay_invoice(
            factory, project_id=project, invoice_id=invoice_id,
            encryption_key=key, client_factory=Provider,
        )
        async with system_project_session_scope(factory, project) as session:
            sub = await session.scalar(select(Subscription).where(
                Subscription.project_id == project,
            ))
            grant = await session.scalar(select(EntitlementGrant).where(
                EntitlementGrant.source_id == invoice_id,
                EntitlementGrant.project_id == project,
            ))
            sub_id = sub.id
            grant_id = grant.id
            assert grant.valid_until is None
        with pytest.raises(ValueError, match="Оплаченный доступ"):
            await revoke_owner_grant(
                factory, actor_id=owner, project_id=project, grant_id=grant_id,
            )
        await end_owner_subscription(
            factory, actor_id=owner, project_id=project,
            subscription_id=sub_id,
        )
        async with system_project_session_scope(factory, project) as session:
            sub = await session.get(Subscription, sub_id)
            grant = await session.get(EntitlementGrant, grant_id)
            assert sub.status == "cancelled"
            assert grant.status == "revoked"
            assert grant.revoked_at is not None
    finally:
        await engine.dispose()


async def test_paid_crypto_channel_cannot_be_deleted_after_plan_resource_edit():
    """A crypto-only sold channel must survive even after plan links are edited."""
    _reset_provider()
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        owner, project, bot_id, _ = await _shop(factory, key)
        async with system_project_session_scope(factory, project) as session:
            resource_id = await session.scalar(select(Resource.id).where(
                Resource.project_id == project,
            ))
            plan = Plan(
                project_id=project, name="Crypto channel",
                duration_value=1, duration_unit="month", status="active",
            )
            session.add(plan)
            await session.flush()
            price = PlanPrice(
                project_id=project, plan_id=plan.id,
                currency_code="RUB", version=1,
                amount=Decimal("250"), status="active",
            )
            session.add_all([
                price,
                PlanResource(
                    project_id=project, plan_id=plan.id,
                    resource_id=resource_id,
                ),
            ])
            await session.flush()
            price_id, plan_id = price.id, plan.id

        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=_tid(),
            kind="subscription", target_id=price_id, key=key,
        )
        settled = await settle_crypto_pay_invoice(
            factory, project_id=project, invoice_id=invoice_id,
            encryption_key=key, client_factory=Provider,
        )
        assert settled.changed
        async with system_project_session_scope(factory, project) as session:
            grant = await session.scalar(select(EntitlementGrant).where(
                EntitlementGrant.project_id == project,
                EntitlementGrant.source_id == invoice_id,
            ))
            assert grant is not None and grant.resource_id == resource_id
            linked = await session.scalar(select(PlanResource).where(
                PlanResource.project_id == project,
                PlanResource.plan_id == plan_id,
                PlanResource.resource_id == resource_id,
            ))
            await session.delete(linked)

        # PlanResource is gone. The frozen grant is still historical evidence:
        # never hide this channel or archive it as an "unsold" channel.
        with pytest.raises(ValueError, match="подтверждённые покупки"):
            await remove_channel_without_paid_purchases(
                factory, actor_id=owner,
                project_id=project, resource_id=resource_id,
            )
        async with system_project_session_scope(factory, project) as session:
            resource = await session.get(Resource, resource_id)
            assert resource.removed_at is None
            grant = await session.scalar(select(EntitlementGrant).where(
                EntitlementGrant.source_id == invoice_id,
                EntitlementGrant.project_id == project,
            ))
            assert grant is not None and grant.source == "crypto_payment"
    finally:
        await engine.dispose()
