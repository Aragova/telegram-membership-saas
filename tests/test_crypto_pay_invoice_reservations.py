"""Crypto Pay reserved/issued invoices are not payment or delivery evidence."""
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4
import os

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.crypto_pay.checkout import (
    inspect_provider_paid_invoice,
    issue_reserved_crypto_invoice,
    reserve_crypto_invoice,
)
from telegram_membership_saas.modules.crypto_pay.client import CryptoPayError
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayInvoice
from telegram_membership_saas.modules.crypto_pay.service import connect_crypto_pay
from telegram_membership_saas.modules.file_sales.manual_models import FileManualPrice
from telegram_membership_saas.modules.file_sales.models import (
    FileAsset,
    FileFolder,
    FileProduct,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.resources import Resource

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated PostgreSQL required for invoice/RLS tests",
)
TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEXT_TOKEN = "123456789:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


class Provider:
    created = []
    invoice_data = {}

    def __init__(self, token: str, *, testnet: bool):
        self.token = token
        self.testnet = testnet

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get_me(self):
        return {"name": "Test merchant"}

    async def create_invoice(self, *, currency, amount, payload, description):
        self.created.append((self.token, currency, amount, payload, description))
        provider_id = len(self.created) + 1100
        self.invoice_data[provider_id] = {
            "invoice_id": provider_id, "payload": payload,
            "currency_type": "fiat", "fiat": currency,
            "amount": format(amount, "f"), "status": "paid",
            "paid_at": datetime.now(UTC).isoformat(),
        }
        return {
            **self.invoice_data[provider_id], "status": "active",
            "bot_invoice_url": "https://t.me/CryptoTestnetBot?start=IV123",
        }

    async def get_invoices(self, invoice_ids):
        return tuple(self.invoice_data[n] for n in invoice_ids)


async def _fixture(factory, *, owner_id, project_id, bot_id, key):
    app = await connect_crypto_pay(
        factory, project_id=project_id, bot_id=bot_id, actor_id=owner_id,
        api_token=TOKEN, encryption_key=key, testnet=True,
        client_factory=Provider,
    )
    async with system_project_session_scope(factory, project_id) as session:
        folder = FileFolder(project_id=project_id, name="Folder", status="active")
        session.add(folder)
        await session.flush()
        product = FileProduct(
            project_id=project_id, folder_id=folder.id,
            telegram_bot_id=bot_id, name="File", status="published",
        )
        session.add(product)
        await session.flush()
        session.add_all([
            FileAsset(
                project_id=project_id, product_id=product.id, kind="document",
                file_id="test-file-id", sort_order=0,
            ),
            FileManualPrice(
                project_id=project_id, product_id=product.id,
                currency_code="RUB", amount=Decimal("55.00"),
            ),
        ])
        return app, product.id


async def test_crypto_file_invoice_snapshots_and_verifies_only_old_app():
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        owner, _, project, bot_uuid, _ = await _project(factory, _tid())
        async with system_project_session_scope(factory, project) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
        old_app, product_id = await _fixture(
            factory, owner_id=owner, project_id=project, bot_id=bot_id, key=key,
        )
        buyer = _tid()
        invoice_id = await reserve_crypto_invoice(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer,
            kind="file", target_id=product_id, currency="RUB",
        )
        async with system_project_session_scope(factory, project) as session:
            price = await session.scalar(select(FileManualPrice).where(
                FileManualPrice.product_id == product_id,
                FileManualPrice.currency_code == "RUB",
            ))
            price.amount = Decimal("99.00")
        with pytest.raises(PermissionError):
            await issue_reserved_crypto_invoice(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=_tid(), invoice_id=invoice_id,
                encryption_key=key, client_factory=Provider,
            )
        issued = await issue_reserved_crypto_invoice(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer,
            invoice_id=invoice_id, encryption_key=key, client_factory=Provider,
        )
        assert issued.amount == Decimal("55.00")
        assert len(Provider.created) == 1
        repeated = await issue_reserved_crypto_invoice(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer,
            invoice_id=invoice_id, encryption_key=key, client_factory=Provider,
        )
        assert repeated == issued
        assert len(Provider.created) == 1
        await connect_crypto_pay(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
            api_token=NEXT_TOKEN, encryption_key=key, testnet=True,
            client_factory=Provider,
        )
        receipt = await inspect_provider_paid_invoice(
            factory, project_id=project, invoice_id=invoice_id,
            encryption_key=key, client_factory=Provider,
        )
        assert receipt.amount == Decimal("55.00")
        assert Provider.created[0][0] == TOKEN
        async with system_project_session_scope(factory, project) as session:
            row = await session.scalar(select(CryptoPayInvoice).where(
                CryptoPayInvoice.id == invoice_id,
            ))
            assert row.app_id == old_app.app_id
            assert row.status == "issued"
            assert row.paid_at is None  # Never claim service before atomic fulfillment.
            assert row.provider_invoice_id == 1101
            assert row.telegram_user_id == buyer
        Provider.invoice_data[1101]["amount"] = "55.01"
        with pytest.raises(CryptoPayError):
            await inspect_provider_paid_invoice(
                factory, project_id=project, invoice_id=invoice_id,
                encryption_key=key, client_factory=Provider,
            )
    finally:
        await engine.dispose()


async def test_crypto_subscription_reservation_fails_closed_on_rotation():
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        owner, _, project, bot_uuid, _ = await _project(factory, _tid())
        async with system_project_session_scope(factory, project) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
        await _fixture(
            factory, owner_id=owner, project_id=project, bot_id=bot_id, key=key,
        )
        async with system_project_session_scope(factory, project) as session:
            resource = await session.scalar(select(Resource).where(
                Resource.project_id == project,
            ))
            plan = Plan(
                project_id=project, name="One month",
                duration_value=1, duration_unit="month", status="active",
            )
            session.add(plan)
            await session.flush()
            price = PlanPrice(
                project_id=project, plan_id=plan.id,
                currency_code="RUB", version=1,
                amount=Decimal("250.00"), status="active",
            )
            session.add_all((
                PlanResource(project_id=project, plan_id=plan.id, resource_id=resource.id),
                price,
            ))
            await session.flush()
            price_id = price.id
        buyer = _tid()
        invoice_id = await reserve_crypto_invoice(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer,
            kind="subscription", target_id=price_id, currency="RUB",
        )
        async with system_project_session_scope(factory, project) as session:
            row = await session.scalar(select(CryptoPayInvoice).where(
                CryptoPayInvoice.id == invoice_id,
            ))
            assert row.customer_id is not None
            assert row.plan_price_id == price_id
            assert row.duration_unit == "month"
            assert row.status == "reserved"
        await connect_crypto_pay(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
            api_token=NEXT_TOKEN, encryption_key=key, testnet=True,
            client_factory=Provider,
        )
        with pytest.raises(PermissionError, match="replaced"):
            await issue_reserved_crypto_invoice(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer, invoice_id=invoice_id,
                encryption_key=key, client_factory=Provider,
            )
        assert not Provider.created
    finally:
        await engine.dispose()
