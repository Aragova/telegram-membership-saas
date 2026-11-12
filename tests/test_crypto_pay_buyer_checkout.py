"""Mainnet Crypto Pay appears in existing buyer storefronts only when enabled."""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_crypto_pay_atomic_fulfillment import _issue, _shop
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
from telegram_membership_saas.modules.crypto_pay.checkout import (
    issue_reserved_crypto_invoice,
    reserve_crypto_invoice,
)
from telegram_membership_saas.modules.crypto_pay.models import (
    CryptoPayApp,
    CryptoPayInvoice,
)
from telegram_membership_saas.modules.crypto_pay.settlement import (
    settle_crypto_pay_invoice,
)
from telegram_membership_saas.modules.file_sales.manual_handlers import (
    show_file_payment_options,
)
from telegram_membership_saas.modules.file_sales.manual_service import (
    available_price_currencies,
    create_manual_order,
    price_editor_options,
    set_manual_price,
)
from telegram_membership_saas.modules.file_sales.models import FileProduct
from telegram_membership_saas.modules.file_sales.payment_controls import (
    AUTO_CRYPTO_ID,
    list_file_payment_controls,
    toggle_file_payment_method,
)
from telegram_membership_saas.modules.file_sales.service import (
    create_stars_order,
    paid_file_purchase_id,
    precheckout_allowed,
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
        # Local reservation age does not prove provider expiry. An already
        # issued invoice 29+ minutes old must not become a second charge.
        async with system_project_session_scope(factory, project) as session:
            invoice = await session.get(CryptoPayInvoice, first.id)
            invoice.created_at = datetime.now(UTC) - timedelta(minutes=35)
        still_original = await begin_buyer_crypto_checkout(factory, **kwargs)
        assert still_original.id == first.id
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
        # Reserve while enabled, then switch off before provider I/O.
        # Old callback and stale reserved invoices must NOT issue charges.
        uid = _tid()
        unpaid_id = await reserve_crypto_invoice(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=uid, kind="file", target_id=product,
            currency="RUB", require_mainnet=True,
        )
        assert not await toggle_file_payment_method(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, method_id=AUTO_CRYPTO_ID,
        )
        with pytest.raises(PermissionError, match="disabled"):
            await reserve_crypto_invoice(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=uid, kind="file", target_id=product,
                currency="RUB", require_mainnet=True,
            )
        with pytest.raises(PermissionError, match="disabled"):
            await issue_reserved_crypto_invoice(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=uid, invoice_id=unpaid_id,
                encryption_key=key, require_mainnet=True,
                client_factory=MainnetProvider,
            )
        assert not Provider.created
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



async def test_paid_crypto_file_cannot_be_billed_again_by_crypto_manual_or_stars(
    monkeypatch,
):
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product_id = await _shop(factory, key)
        buyer_id = _tid()
        async with system_project_session_scope(factory, project) as session:
            item = await session.get(FileProduct, product_id)
            item.stars_price = 100
        # An already issued Stars invoice must stop at precheckout once
        # Crypto Pay has settled the same digital good.
        old_stars = await create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
        )
        assert not old_stars.paid
        crypto_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer_id,
            kind="file", target_id=product_id, key=key,
        )
        assert (await settle_crypto_pay_invoice(
            factory, project_id=project, invoice_id=crypto_id,
            encryption_key=key, client_factory=Provider,
        )).changed
        assert await paid_file_purchase_id(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
        ) == crypto_id
        assert not await precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, invoice_payload=old_stars.invoice_payload,
            amount=100, currency="XTR",
        )
        manual, paid = await create_manual_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
            currency="RUB", method_id=uuid4(),
        )
        assert paid and manual.id == crypto_id
        stars = await create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
        )
        assert stars.paid and stars.id == crypto_id

        async with system_project_session_scope(factory, project) as session:
            app = await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.project_id == project,
            ))
            app.environment = "mainnet"
        enabled_settings = Settings(
            bot_token_encryption_key=key, crypto_pay_buyer_enabled=True,
        )
        monkeypatch.setattr(buyer, "get_settings", lambda: enabled_settings)
        monkeypatch.setattr(
            "telegram_membership_saas.config.get_settings",
            lambda: enabled_settings,
        )
        with pytest.raises(ValueError, match="already been purchased"):
            await reserve_crypto_invoice(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_id, kind="file",
                target_id=product_id, currency="RUB", require_mainnet=True,
            )

        class FileBot:
            id = bot_id
            def __init__(self):
                self.sent = []
            async def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text, kwargs))

        bot = FileBot()
        await show_file_payment_options(
            bot, factory=factory, project_id=project,
            buyer_id=buyer_id, product_id=product_id,
        )
        assert len(bot.sent) == 1
        markup = bot.sent[0][2]["reply_markup"]
        assert markup.inline_keyboard[0][0].callback_data.startswith("fm:get:")
        assert all("Crypto Pay" not in button.text
                   for row in markup.inline_keyboard for button in row)
    finally:
        await engine.dispose()



async def test_one_crypto_file_quote_survives_currency_and_price_changes(monkeypatch):
    """One-time files cannot have simultaneous payable USD and RUB invoices."""
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        owner, project, bot_id, product_id = await _shop(factory, key)
        async with system_project_session_scope(factory, project) as session:
            app = await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.project_id == project,
            ))
            app.environment = "mainnet"
        monkeypatch.setattr(buyer, "get_settings", lambda: Settings(
            bot_token_encryption_key=key, crypto_pay_buyer_enabled=True,
        ))
        await set_manual_price(
            factory, project_id=project, bot_id=bot_id,
            product_id=product_id, actor_id=owner,
            currency="USD", value="7.50",
        )
        options = dict(await price_editor_options(
            factory, project_id=project, bot_id=bot_id,
            product_id=product_id, actor_id=owner,
        ))
        assert options["USD"] == Decimal("7.50")
        def kwargs(code):
            return {
                "project_id": project, "bot_id": bot_id, "buyer_id": uid,
                "kind": "file", "target_id": product_id,
                "currency": code, "encryption_key": key,
                "client_factory": MainnetProvider,
            }
        uid = _tid()
        first = await begin_buyer_crypto_checkout(factory, **kwargs("RUB"))
        second = await begin_buyer_crypto_checkout(factory, **kwargs("USD"))
        assert first.id == second.id
        assert second.currency == "RUB"
        assert second.amount == Decimal("55.00")
        assert len(Provider.created) == 1
        await set_manual_price(
            factory, project_id=project, bot_id=bot_id,
            product_id=product_id, actor_id=owner,
            currency="RUB", value="64.99",
        )
        third = await begin_buyer_crypto_checkout(factory, **kwargs("RUB"))
        assert third.id == first.id
        assert third.amount == Decimal("55.00")
        assert len(Provider.created) == 1
    finally:
        await engine.dispose()


async def test_outstanding_crypto_invoice_blocks_stars_and_manual_file_checkout():
    """Neither an old Stars link nor a new purchase may double charge a file."""
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product_id = await _shop(factory, key)
        buyer_id = _tid()
        other_buyer = _tid()
        async with system_project_session_scope(factory, project) as session:
            product = await session.get(FileProduct, product_id)
            product.stars_price = 50

        # Native Stars invoice was displayed first. It must stop at the
        # Telegram precheckout boundary if the buyer later starts Crypto Pay.
        pending_stars = await create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
        )
        assert not pending_stars.paid
        assert await precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, invoice_payload=pending_stars.invoice_payload,
            amount=50, currency="XTR",
        )
        crypto_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer_id,
            kind="file", target_id=product_id, key=key,
        )
        assert not await precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, invoice_payload=pending_stars.invoice_payload,
            amount=50, currency="XTR",
        )
        with pytest.raises(ValueError, match="Crypto Pay invoice"):
            await create_stars_order(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_id, product_id=product_id,
            )
        with pytest.raises(ValueError, match="Crypto Pay invoice"):
            await create_manual_order(
                factory, project_id=project, bot_id=bot_id,
                buyer_id=buyer_id, product_id=product_id,
                currency="RUB", method_id=uuid4(),
            )
        # Another customer's purchase of the same file is independent.
        independent = await create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=other_buyer, product_id=product_id,
        )
        assert not independent.paid

        # 'issuing' may mean Crypto Pay already accepted the payment,
        # even if the bot lost its response. Never treat it as safe.
        async with system_project_session_scope(factory, project) as session:
            record = await session.get(CryptoPayInvoice, crypto_id)
            record.status = "issuing"
        assert not await precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, invoice_payload=pending_stars.invoice_payload,
            amount=50, currency="XTR",
        )

        # ONLY the provider reconciliation worker sets terminal expiry.
        async with system_project_session_scope(factory, project) as session:
            record = await session.get(CryptoPayInvoice, crypto_id)
            record.status = "expired"
            record.invoice_url = None
        assert await precheckout_allowed(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, invoice_payload=pending_stars.invoice_payload,
            amount=50, currency="XTR",
        )
        reopened = await create_stars_order(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
        )
        assert reopened.id == pending_stars.id
    finally:
        await engine.dispose()
