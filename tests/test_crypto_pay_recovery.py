"""Real Postgres + fake provider/Telegram checks for Crypto Pay recovery.

The worker NEVER assumes a Telegram button or screenshot is proof of payment.
Each test uses the project's actual reservation and original merchant app.
"""
import asyncio
import os
from datetime import UTC, datetime, timedelta

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
from telegram_membership_saas.modules.crypto_pay.checkout import (
    issue_reserved_crypto_invoice,
    reserve_crypto_invoice,
)
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayInvoice
from telegram_membership_saas.modules.crypto_pay.recovery import (
    reconcile_crypto_pay_once,
)
from telegram_membership_saas.modules.entitlements import EntitlementGrant

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated real PostgreSQL/RLS required",
)


class TelegramStub:
    def __init__(self, bot_id: int):
        self.id = bot_id
        self.messages: list[tuple[int, str]] = []
        self.files: list[tuple[int, str]] = []
        self.fail_media_once = False

    async def send_message(self, user_id, text, **kwargs):
        self.messages.append((user_id, text))

    async def send_document(self, user_id, file_id, **kwargs):
        if self.fail_media_once:
            self.fail_media_once = False
            raise RuntimeError("Simulated Telegram media send error")
        self.files.append((user_id, file_id))

    # _deliver builds a method lookup containing every Telegram media type.
    send_photo = send_document
    send_video = send_document
    send_audio = send_document
    send_animation = send_document


def _provider_paid(paid: bool) -> None:
    for invoice in Provider.invoice_data.values():
        invoice["status"] = "paid" if paid else "active"
        if paid:
            invoice["paid_at"] = datetime.now(UTC).isoformat()
        else:
            invoice.pop("paid_at", None)
        invoice["bot_invoice_url"] = "https://t.me/CryptoTestnetBot?start=IV123"


async def test_crypto_worker_settles_paid_file_and_delivers_only_once():
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product = await _shop(factory, key)
        buyer = _tid()
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer,
            kind="file", target_id=product, key=key,
        )
        bot = TelegramStub(bot_id)
        worked = await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            client_factory=Provider,
        )
        assert worked
        assert bot.files == [(buyer, "test-file-id")]
        assert len(bot.messages) == 2
        async with system_project_session_scope(factory, project) as session:
            record = await session.get(CryptoPayInvoice, invoice_id)
            assert record.status == "paid"
            assert record.last_delivered_at is not None
            assert record.delivery_attempt_count == 1
            assert record.next_delivery_at is not None
        again = await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            client_factory=Provider,
        )
        assert not again
        assert bot.files == [(buyer, "test-file-id")]
    finally:
        await engine.dispose()


async def test_crypto_worker_active_invoice_never_delivers_until_provider_confirms():
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product = await _shop(factory, key)
        buyer = _tid()
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer,
            kind="file", target_id=product, key=key,
        )
        _provider_paid(False)
        bot = TelegramStub(bot_id)
        first = await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            client_factory=Provider,
        )
        assert not first
        assert bot.files == []
        async with system_project_session_scope(factory, project) as session:
            invoice = await session.get(CryptoPayInvoice, invoice_id)
            assert invoice.status == "issued" and invoice.paid_at is None
            assert invoice.next_check_at is not None
        _provider_paid(True)
        second = await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            now=datetime.now(UTC) + timedelta(minutes=3),
            client_factory=Provider,
        )
        assert second
        assert bot.files == [(buyer, "test-file-id")]
    finally:
        await engine.dispose()


async def test_crypto_worker_recovers_uncertain_paid_invoice_without_recreating_charge():
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product = await _shop(factory, key)
        buyer = _tid()
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer,
            kind="file", target_id=product, key=key,
        )
        # Simulate DB outage after Crypto Pay accepted the invoice.
        async with system_project_session_scope(factory, project) as session:
            record = await session.get(CryptoPayInvoice, invoice_id)
            record.status = "issuing"
            record.provider_invoice_id = None
            record.invoice_url = None
        _provider_paid(True)
        bot = TelegramStub(bot_id)
        assert await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            client_factory=Provider,
        )
        assert len(Provider.created) == 1
        assert bot.files == [(buyer, "test-file-id")]
        async with system_project_session_scope(factory, project) as session:
            record = await session.get(CryptoPayInvoice, invoice_id)
            assert record.status == "paid"
            assert record.provider_invoice_id == 1101
    finally:
        await engine.dispose()


async def test_crypto_worker_expired_provider_invoice_cannot_grant_file_or_access():
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product = await _shop(factory, key)
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=_tid(),
            kind="file", target_id=product, key=key,
        )
        for item in Provider.invoice_data.values():
            item["status"] = "expired"
            item.pop("paid_at", None)
        bot = TelegramStub(bot_id)
        assert await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            client_factory=Provider,
        )
        assert not bot.files
        async with system_project_session_scope(factory, project) as session:
            record = await session.get(CryptoPayInvoice, invoice_id)
            assert record.status == "expired"
            assert record.invoice_url is None
            assert record.paid_at is None
            assert not (await session.scalars(select(EntitlementGrant).where(
                EntitlementGrant.project_id == project,
            ))).all()
    finally:
        await engine.dispose()



async def test_recovered_active_invoice_gets_early_paid_poll_not_twenty_minutes():
    """Successful lookup must not retain the 20-minute 'issuing' backoff."""
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product = await _shop(factory, key)
        buyer = _tid()
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer,
            kind="file", target_id=product, key=key,
        )
        async with system_project_session_scope(factory, project) as session:
            invoice = await session.get(CryptoPayInvoice, invoice_id)
            invoice.status = "issuing"
            invoice.provider_invoice_id = None
            invoice.invoice_url = None
        _provider_paid(False)
        bot = TelegramStub(bot_id)
        assert await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            client_factory=Provider,
        )
        async with system_project_session_scope(factory, project) as session:
            invoice = await session.get(CryptoPayInvoice, invoice_id)
            assert invoice.status == "issued"
            assert invoice.next_check_at is None
        assert bot.files == []
        _provider_paid(True)
        assert await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            now=datetime.now(UTC) + timedelta(minutes=2),
            client_factory=Provider,
        )
        assert bot.files == [(buyer, "test-file-id")]
    finally:
        await engine.dispose()


async def test_successful_issuance_clears_concurrent_uncertainty_claim():
    """Late successful createInvoice must restore normal due time."""
    Provider.created = []
    Provider.invoice_data = {}
    entered = asyncio.Event()
    release = asyncio.Event()

    class DelayedProvider(Provider):
        async def create_invoice(self, *, currency, amount, payload, description):
            entered.set()
            await release.wait()
            return await super().create_invoice(
                currency=currency, amount=amount,
                payload=payload, description=description,
            )

    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product = await _shop(factory, key)
        buyer = _tid()
        invoice_id = await reserve_crypto_invoice(
            factory, project_id=project, bot_id=bot_id, buyer_id=buyer,
            kind="file", target_id=product, currency="RUB",
        )
        issuance = asyncio.create_task(issue_reserved_crypto_invoice(
            factory, project_id=project, bot_id=bot_id,
            buyer_id=buyer, invoice_id=invoice_id, encryption_key=key,
            client_factory=DelayedProvider,
        ))
        await asyncio.wait_for(entered.wait(), timeout=10)
        async with system_project_session_scope(factory, project) as session:
            record = await session.get(CryptoPayInvoice, invoice_id)
            assert record.status == "issuing"
            record.next_check_at = datetime.now(UTC) + timedelta(minutes=20)
        release.set()
        issued = await issuance
        assert issued.id == invoice_id
        async with system_project_session_scope(factory, project) as session:
            record = await session.get(CryptoPayInvoice, invoice_id)
            assert record.status == "issued"
            assert record.next_check_at is None
    finally:
        release.set()
        await engine.dispose()


async def test_failed_crypto_media_send_recovers_after_backoff():
    """A paid but not fully delivered file stays retryable after a crash."""
    Provider.created = []
    Provider.invoice_data = {}
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project, bot_id, product = await _shop(factory, key)
        buyer = _tid()
        invoice_id = await _issue(
            factory, project=project, bot_id=bot_id, buyer=buyer,
            kind="file", target_id=product, key=key,
        )
        bot = TelegramStub(bot_id)
        bot.fail_media_once = True
        assert await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            client_factory=Provider,
        )
        assert bot.files == []
        async with system_project_session_scope(factory, project) as session:
            paid = await session.get(CryptoPayInvoice, invoice_id)
            assert paid.status == "paid"
            assert paid.last_delivered_at is None
            assert paid.delivery_attempt_count == 1
        assert await reconcile_crypto_pay_once(
            factory, project_id=project, bot=bot, encryption_key=key,
            now=datetime.now(UTC) + timedelta(minutes=5),
            client_factory=Provider,
        )
        assert bot.files == [(buyer, "test-file-id")]
        async with system_project_session_scope(factory, project) as session:
            paid = await session.get(CryptoPayInvoice, invoice_id)
            assert paid.last_delivered_at is not None
            assert paid.delivery_attempt_count == 2
    finally:
        await engine.dispose()
