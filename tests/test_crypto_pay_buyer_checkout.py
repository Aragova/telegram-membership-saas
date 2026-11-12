"""Mainnet Crypto Pay appears in existing buyer storefronts only when enabled."""
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_crypto_pay_atomic_fulfillment import _shop
from test_crypto_pay_invoice_reservations import Provider, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
)
from telegram_membership_saas.modules.crypto_pay import buyer
from telegram_membership_saas.modules.crypto_pay.buyer import (
    CRYPTO_METHOD_ID,
    BuyerCryptoInvoice,
    begin_buyer_crypto_checkout,
    crypto_merchant_ready,
    file_crypto_prices,
)
from telegram_membership_saas.modules.crypto_pay.buyer_ui import (
    send_buyer_crypto_invoice_screen,
)
from telegram_membership_saas.modules.crypto_pay.models import (
    CryptoPayApp,
    CryptoPayInvoice,
)
from telegram_membership_saas.modules.file_sales.manual_service import (
    available_price_currencies,
    price_editor_options,
    set_manual_price,
)
from telegram_membership_saas.modules.file_sales.payment_controls import (
    AUTO_CRYPTO_ID,
    list_file_payment_controls,
    toggle_file_payment_method,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    list_buyer_offers,
    offer_screen,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated non-bypass PostgreSQL/RLS required",
)


class MainnetProvider(Provider):
    async def create_invoice(self, *, currency, amount, payload, description):
        invoice = await super().create_invoice(
            currency=currency, amount=amount, payload=payload,
            description=description,
        )
        invoice["bot_invoice_url"] = "https://t.me/CryptoBot?start=IV123"
        return invoice


class ScreenBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))


async def _prepare(factory, project_id, *, mainnet: bool):
    async with system_project_session_scope(factory, project_id) as session:
        app = await session.scalar(select(CryptoPayApp).where(
            CryptoPayApp.project_id == project_id,
        ))
        app.environment = "mainnet" if mainnet else "testnet"
        resource_id = await session.scalar(select(Resource.id).where(
            Resource.project_id == project_id,
        ))
        plan = Plan(
            project_id=project_id, name="Crypto monthly",
            duration_value=1, duration_unit="month", status="active",
        )
        session.add(plan)
        await session.flush()
        price = PlanPrice(
            project_id=project_id, plan_id=plan.id,
            currency_code="RUB", version=1,
            amount=Decimal("250"), status="active",
        )
        session.add_all([
            price, PlanResource(
                project_id=project_id, plan_id=plan.id, resource_id=resource_id,
            ),
        ])
        await session.flush()
        return price.id


async def test_crypto_only_subscription_and_no_duplicate_invoice(monkeypatch):
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, _ = await _shop(factory, key)
        price_id = await _prepare(factory, project, mainnet=False)
        monkeypatch.setattr(buyer, "get_settings", lambda: Settings(
            bot_token_encryption_key=key, crypto_pay_buyer_enabled=True,
        ))
        async with system_project_session_scope(factory, project) as session:
            assert not await crypto_merchant_ready(
                session, project_id=project, bot_id=bot_id,
            )
            assert not await list_buyer_offers(
                session, project_id=project, bot_id=bot_id,
            )
            app = await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.project_id == project,
            ))
            app.environment = "mainnet"

        async with system_project_session_scope(factory, project) as session:
            offers = await list_buyer_offers(
                session, project_id=project, bot_id=bot_id,
            )
            assert len(offers) == 1
            assert len(offers[0].methods) == 1
            assert offers[0].methods[0].payment_method_id == CRYPTO_METHOD_ID
            _, buttons = offer_screen(offers, price_id=price_id)
            assert sum("Crypto Pay" in b.text
                       for row in buttons.inline_keyboard for b in row) == 1

        uid = _tid()
        kwargs = dict(
            project_id=project, bot_id=bot_id, buyer_id=uid,
            kind="subscription", target_id=price_id, encryption_key=key,
            client_factory=MainnetProvider,
        )
        first = await begin_buyer_crypto_checkout(factory, **kwargs)
        second = await begin_buyer_crypto_checkout(factory, **kwargs)
        assert first == second and first.url.endswith("IV123")
        assert len(Provider.created) == 1
        async with system_project_session_scope(factory, project) as session:
            invoices = (await session.scalars(select(CryptoPayInvoice).where(
                CryptoPayInvoice.project_id == project,
            ))).all()
            assert len(invoices) == 1 and invoices[0].status == "issued"
    finally:
        await engine.dispose()


async def test_crypto_file_prices_without_manual_bank_and_owner_toggle(monkeypatch):
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        owner, project, bot_id, product = await _shop(factory, key)
        monkeypatch.setattr(buyer, "get_settings", lambda: Settings(
            bot_token_encryption_key=key, crypto_pay_buyer_enabled=True,
        ))
        async with system_project_session_scope(factory, project) as session:
            app = await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.project_id == project,
            ))
            app.environment = "mainnet"
        assert "RUB" in await available_price_currencies(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
        )
        assert ("RUB", Decimal("55")) in await price_editor_options(
            factory, project_id=project, bot_id=bot_id,
            product_id=product, actor_id=owner,
        )
        assert await set_manual_price(
            factory, project_id=project, bot_id=bot_id,
            product_id=product, actor_id=owner,
            currency="RUB", value="59.90",
        ) == Decimal("59.90")
        async with system_project_session_scope(factory, project) as session:
            offers = await file_crypto_prices(
                session, project_id=project, bot_id=bot_id,
                product_id=product,
            )
            assert len(offers) == 1 and offers[0].amount == Decimal("59.90")
        controls = await list_file_payment_controls(
            factory, project_id=project, bot_id=bot_id, actor_id=owner,
        )
        assert any(c.method_id == AUTO_CRYPTO_ID for c in controls)
        assert not await toggle_file_payment_method(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, method_id=AUTO_CRYPTO_ID,
        )
        async with system_project_session_scope(factory, project) as session:
            assert not await file_crypto_prices(
                session, project_id=project, bot_id=bot_id,
                product_id=product,
            )
    finally:
        await engine.dispose()


async def test_crypto_checkout_fallback_has_real_link_and_only_back():
    bot = ScreenBot()
    invoice = BuyerCryptoInvoice(
        id=uuid4(), url="https://t.me/CryptoBot?start=IV123",
        name="Buy <file>", kind="file",
        amount=Decimal("59.90"), currency="RUB",
        duration_value=None, duration_unit=None,
    )
    await send_buyer_crypto_invoice_screen(
        bot, buyer_id=12345, invoice=invoice,
        back_callback="fs:pay:AAAAAAAAAAAAAAAAAAAAAA",
    )
    assert len(bot.messages) == 1
    _, text, kwargs = bot.messages[0]
    assert "Buy &lt;file&gt;" in text
    buttons = kwargs["reply_markup"].inline_keyboard
    assert buttons[0][0].url == invoice.url
    assert buttons[1][0].callback_data.startswith("fs:pay:")
