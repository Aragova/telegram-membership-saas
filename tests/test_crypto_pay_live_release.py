"""Buyer-to-fulfillment release contract: one Crypto Pay rail, both products.

Real migrated PostgreSQL/RLS, fake mainnet provider/Telegram. No money moves.
The very same checkout and recovery entry points run in the light100 worker.
"""
import os
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_crypto_pay_atomic_fulfillment import _shop
from test_crypto_pay_buyer_checkout import MainnetProvider, _prepare
from test_crypto_pay_invoice_reservations import Provider, _tid
from test_crypto_pay_recovery import TelegramStub

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
)
from telegram_membership_saas.modules.crypto_pay import buyer
from telegram_membership_saas.modules.crypto_pay.buyer import (
    CRYPTO_METHOD_ID,
    begin_buyer_crypto_checkout,
)
from telegram_membership_saas.modules.crypto_pay.buyer_ui import (
    send_buyer_crypto_invoice_screen,
)
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayApp, CryptoPayInvoice
from telegram_membership_saas.modules.crypto_pay.recovery import (
    reconcile_crypto_pay_once,
)
from telegram_membership_saas.modules.entitlements import EntitlementGrant
from telegram_membership_saas.modules.file_sales.manual_handlers import (
    show_file_payment_options,
)
from telegram_membership_saas.modules.file_sales.service import paid_file_purchase_id
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    list_buyer_offers,
    offer_screen,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Real migrated PostgreSQL and non-bypass runtime RLS required",
)


class BuyerTelegram(TelegramStub):
    def __init__(self, bot_id: int):
        super().__init__(bot_id)
        self.cards: list[tuple[int, str, dict]] = []

    async def send_message(self, user_id, text, **kwargs):
        self.cards.append((user_id, text, kwargs))
        await super().send_message(user_id, text, **kwargs)


def _has_crypto_button(markup) -> bool:
    return any(
        "Crypto Pay" in button.text
        for row in markup.inline_keyboard for button in row
    )


async def test_release_mainnet_file_and_subscription_complete_through_one_worker(
    monkeypatch,
):
    """Offer -> real mainnet invoice -> provider receipt -> grant/file, once."""
    Provider.created = []
    Provider.invoice_data = {}
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    config = Settings(
        bot_token_encryption_key=key, crypto_pay_buyer_enabled=True,
    )
    monkeypatch.setattr(buyer, "get_settings", lambda: config)
    monkeypatch.setattr(
        "telegram_membership_saas.config.get_settings", lambda: config,
    )

    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        _, project_id, bot_id, product_id = await _shop(factory, key)
        plan_price_id = await _prepare(factory, project_id, mainnet=True)
        buyer_id = _tid()
        telegram = BuyerTelegram(bot_id)

        async with system_project_session_scope(factory, project_id) as session:
            offers = await list_buyer_offers(
                session, project_id=project_id, bot_id=bot_id,
            )
        assert len(offers) == 1
        assert len(offers[0].methods) == 1
        assert offers[0].methods[0].payment_method_id == CRYPTO_METHOD_ID
        _, subs_markup = offer_screen(offers, price_id=plan_price_id)
        assert _has_crypto_button(subs_markup)

        await show_file_payment_options(
            telegram, factory=factory, project_id=project_id,
            buyer_id=buyer_id, product_id=product_id,
        )
        assert _has_crypto_button(telegram.cards[-1][2]["reply_markup"])

        subscription = await begin_buyer_crypto_checkout(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, kind="subscription",
            target_id=plan_price_id, encryption_key=key,
            client_factory=MainnetProvider,
        )
        file = await begin_buyer_crypto_checkout(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, kind="file", target_id=product_id,
            currency="RUB", encryption_key=key,
            client_factory=MainnetProvider,
        )
        assert subscription.url.startswith("https://t.me/CryptoBot?")
        assert file.url.startswith("https://t.me/CryptoBot?")
        assert len(Provider.created) == 2

        for invoice, back in (
            (subscription, "offer:test:0"),
            (file, "fs:pay:test"),
        ):
            await send_buyer_crypto_invoice_screen(
                telegram, buyer_id=buyer_id, invoice=invoice,
                back_callback=back,
            )
            markup = telegram.cards[-1][2]["reply_markup"]
            assert markup.inline_keyboard[0][0].url == invoice.url
            assert not any("Я оплатил" in button.text or "I paid" in button.text
                           for row in markup.inline_keyboard for button in row)

        # A real provider failure must NOT fabricate a payment or deliver.
        provider_file_id = next(
            k for k, value in Provider.invoice_data.items()
            if value["payload"] == "cp:" + file.id.hex
        )
        # The frozen local payload uses the UUID hex form. Never accept
        # buyer input or screenshots as payment evidence.
        original_paid_at = Provider.invoice_data[provider_file_id].pop("paid_at")
        Provider.invoice_data[provider_file_id].update({
            "status": "active",
            "bot_invoice_url": "https://t.me/CryptoBot?start=IV123",
        })
        assert await reconcile_crypto_pay_once(
            factory, project_id=project_id, bot=telegram,
            encryption_key=key, client_factory=MainnetProvider,
        )
        assert telegram.files == []
        async with system_project_session_scope(factory, project_id) as session:
            file_record = await session.get(CryptoPayInvoice, file.id)
            sub_record = await session.get(CryptoPayInvoice, subscription.id)
            assert file_record.status == "issued"
            assert sub_record.status == "paid"
            assert len((await session.scalars(select(Subscription).where(
                Subscription.project_id == project_id,
            ))).all()) == 1
            grants = (await session.scalars(select(EntitlementGrant).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == subscription.id,
            ))).all()
            assert len(grants) == 1

        Provider.invoice_data[provider_file_id].update({
            "status": "paid", "paid_at": original_paid_at,
        })
        assert await reconcile_crypto_pay_once(
            factory, project_id=project_id, bot=telegram,
            encryption_key=key, client_factory=MainnetProvider,
            now=datetime.now(UTC) + timedelta(minutes=5),
        )
        assert telegram.files == [(buyer_id, "test-file-id")]
        assert await paid_file_purchase_id(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, product_id=product_id,
        ) == file.id
        again = await begin_buyer_crypto_checkout(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer_id, kind="subscription",
            target_id=plan_price_id, encryption_key=key,
            client_factory=MainnetProvider,
        )
        # A new subscription invoice is a legitimate subsequent renewal.
        assert again.id != subscription.id
        assert len(Provider.created) == 3
        await reconcile_crypto_pay_once(
            factory, project_id=project_id, bot=telegram,
            encryption_key=key, client_factory=MainnetProvider,
            now=datetime.now(UTC) + timedelta(minutes=10),
        )
        await reconcile_crypto_pay_once(
            factory, project_id=project_id, bot=telegram,
            encryption_key=key, client_factory=MainnetProvider,
            now=datetime.now(UTC) + timedelta(minutes=15),
        )
        assert telegram.files == [(buyer_id, "test-file-id")]
        async with system_project_session_scope(factory, project_id) as session:
            completed = await session.get(CryptoPayInvoice, file.id)
            assert completed.status == "paid"
            assert completed.last_delivered_at is not None
            assert len((await session.scalars(select(Subscription).where(
                Subscription.project_id == project_id,
            ))).all()) == 1
    finally:
        await engine.dispose()


async def test_release_disabled_or_testnet_never_advertises_live_crypto(
    monkeypatch,
):
    """Emergency-off hides new checkouts; testnet cannot leak to buyers."""
    Provider.created = []
    Provider.invoice_data = {}
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        _, project_id, bot_id, _ = await _shop(factory, key)
        plan_price_id = await _prepare(factory, project_id, mainnet=True)
        disabled = Settings(
            bot_token_encryption_key=key, crypto_pay_buyer_enabled=False,
        )
        monkeypatch.setattr(buyer, "get_settings", lambda: disabled)
        async with system_project_session_scope(factory, project_id) as session:
            assert not await list_buyer_offers(
                session, project_id=project_id, bot_id=bot_id,
            )
        with pytest.raises(PermissionError, match="unavailable"):
            await begin_buyer_crypto_checkout(
                factory, project_id=project_id, bot_id=bot_id,
                buyer_id=_tid(), kind="subscription",
                target_id=plan_price_id, encryption_key=key,
                client_factory=MainnetProvider,
            )
        assert Provider.created == []
        enabled = Settings(
            bot_token_encryption_key=key, crypto_pay_buyer_enabled=True,
        )
        monkeypatch.setattr(buyer, "get_settings", lambda: enabled)
        async with system_project_session_scope(factory, project_id) as session:
            app = await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.project_id == project_id,
            ))
            app.environment = "testnet"
        async with system_project_session_scope(factory, project_id) as session:
            assert not await list_buyer_offers(
                session, project_id=project_id, bot_id=bot_id,
            )
        assert Provider.created == []
    finally:
        await engine.dispose()
