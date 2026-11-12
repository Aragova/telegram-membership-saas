"""Crash-safe Crypto Pay polling and retryable Telegram file delivery.

One worker per connected bot; no additional supervisor or separate subscriber
ledger. Only trusted Crypto Pay getInvoices evidence can settle a payment.
No provider I/O or Telegram sends occur inside a DB transaction.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiogram import Bot
from pydantic import SecretStr
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.localization import recipient_language
from telegram_membership_saas.modules.crypto_pay.checkout import (
    recover_uncertain_crypto_invoice,
)
from telegram_membership_saas.modules.crypto_pay.client import (
    CryptoPayClient,
    CryptoPayError,
    verify_created_invoice_snapshot,
    verify_paid_invoice,
)
from telegram_membership_saas.modules.crypto_pay.models import (
    CryptoPayInvoice,
    CryptoPayPaidUsdRate,
)
from telegram_membership_saas.modules.crypto_pay.service import load_crypto_app_token
from telegram_membership_saas.modules.crypto_pay.settlement import (
    _apply_verified_receipt,
)
from telegram_membership_saas.modules.file_sales.service import (
    mark_file_delivery,
    purchased_assets,
)
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock

log = logging.getLogger(__name__)
MAX_INVOICES_PER_SCAN = 12
MAX_RATE_REPAIRS_PER_SCAN = 3
ISSUING_LIST_PAGES = 3
POLL_SECONDS = 45.0


def _require_utc(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Crypto Pay worker requires an aware UTC timestamp")
    return at.astimezone(UTC)


def _next_check(invoice: CryptoPayInvoice, *, now: datetime) -> datetime:
    if invoice.status == "issuing":
        # The provider offers no idempotency key or payload search: scan
        # only three 100-item pages, at most once every 20 minutes per order.
        return now + timedelta(minutes=20)
    age = now - invoice.created_at
    if age < timedelta(hours=1):
        return now + timedelta(seconds=60)
    if age < timedelta(days=1):
        return now + timedelta(minutes=5)
    return now + timedelta(hours=1)


async def _claim_due_invoice_ids(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, now: datetime,
) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
    """Lock and persist next due time before any provider call."""
    async with system_project_session_scope(factory, project_id) as session:
        base = (
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.telegram_bot_id == bot_id,
            or_(
                CryptoPayInvoice.next_check_at.is_(None),
                CryptoPayInvoice.next_check_at <= now,
            ),
        )
        # Issuance recovery may scan 300 merchant invoices per attempt.
        # Claim only ONE uncertain order; do not postpone 11 more by
        # claiming them all while only processing the first.
        uncertain = (await session.scalars(select(CryptoPayInvoice).where(
            *base, CryptoPayInvoice.status == "issuing",
        ).order_by(
            CryptoPayInvoice.next_check_at.asc().nullsfirst(),
            CryptoPayInvoice.created_at,
        ).limit(1).with_for_update(skip_locked=True))).all()
        payable = (await session.scalars(select(CryptoPayInvoice).where(
            *base, CryptoPayInvoice.status == "issued",
        ).order_by(
            CryptoPayInvoice.next_check_at.asc().nullsfirst(),
            CryptoPayInvoice.created_at,
        ).limit(MAX_INVOICES_PER_SCAN).with_for_update(skip_locked=True))).all()
        issuing: list[UUID] = []
        issued: list[UUID] = []
        for record in (*uncertain, *payable):
            record.next_check_at = _next_check(record, now=now)
            (issuing if record.status == "issuing" else issued).append(record.id)
        await session.flush()
        return tuple(issuing), tuple(issued)


async def _verify_issued_batch(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    invoice_ids: tuple[UUID, ...], key: SecretStr,
    client_factory: Callable[..., CryptoPayClient],
) -> int:
    """Group getInvoices by the ORIGINAL merchant token version.

    One bounded provider call per app instead of polling every invoice
    separately. A mismatched provider item cannot settle a different buyer.
    """
    if not invoice_ids:
        return 0
    grouped: dict[UUID, list[tuple]] = defaultdict(list)
    credentials: dict[UUID, tuple[str, bool]] = {}
    async with system_project_session_scope(factory, project_id) as session:
        for record in (await session.scalars(select(CryptoPayInvoice).where(
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.id.in_(invoice_ids),
            CryptoPayInvoice.status == "issued",
        ))).all():
            if record.provider_invoice_id is None:
                continue
            if record.app_id not in credentials:
                app, token = await load_crypto_app_token(
                    session, project_id=project_id, app_id=record.app_id,
                    encryption_key=key,
                )
                if app.telegram_bot_id != record.telegram_bot_id:
                    raise CryptoPayError("Crypto Pay merchant app/bot mismatch")
                credentials[record.app_id] = (
                    token, app.environment == "testnet",
                )
            grouped[record.app_id].append((
                record.id, record.provider_invoice_id, record.payload,
                record.amount, record.currency_code, record.currency_type,
            ))
    completed = 0
    for app_id, rows in grouped.items():
        token, testnet = credentials[app_id]
        try:
            async with client_factory(token, testnet=testnet) as provider:
                data = await provider.get_invoices(tuple(row[1] for row in rows))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never print raw errors. Transport exceptions can contain tokens.
            log.warning("Crypto Pay invoice read deferred (%s)", type(exc).__name__)
            continue
        by_id = {item["invoice_id"]: item for item in data}
        for invoice_id, provider_id, payload, amount, code, currency_type in rows:
            remote = by_id.get(provider_id)
            if remote is None:
                continue
            try:
                status = remote.get("status")
                if status == "paid":
                    receipt = verify_paid_invoice(
                        remote, invoice_id=provider_id, payload=payload,
                        amount=amount, currency_code=code,
                        currency_type=currency_type,
                    )
                    async with system_project_session_scope(factory, project_id) as session:
                        result = await _apply_verified_receipt(
                            session, project_id=project_id,
                            invoice_id=invoice_id, receipt=receipt,
                        )
                    completed += int(result.changed)
                elif status == "expired":
                    confirmed_id, link = verify_created_invoice_snapshot(
                        remote, payload=payload, amount=amount,
                        currency_code=code, currency_type=currency_type,
                        testnet=testnet,
                    )
                    if confirmed_id != provider_id or link is not None:
                        raise CryptoPayError("Crypto Pay expiration evidence is invalid")
                    async with system_project_session_scope(factory, project_id) as session:
                        await checkout_retirement_lock(session, project_id)
                        record = await session.scalar(select(CryptoPayInvoice).where(
                            CryptoPayInvoice.project_id == project_id,
                            CryptoPayInvoice.id == invoice_id,
                            CryptoPayInvoice.app_id == app_id,
                            CryptoPayInvoice.provider_invoice_id == provider_id,
                        ).with_for_update())
                        if record is not None and record.status == "issued":
                            record.status = "expired"
                            record.invoice_url = None
                            await session.flush()
                            completed += 1
                elif status == "active":
                    confirmed_id, _ = verify_created_invoice_snapshot(
                        remote, payload=payload, amount=amount,
                        currency_code=code, currency_type=currency_type,
                        testnet=testnet,
                    )
                    if confirmed_id != provider_id:
                        raise CryptoPayError("Crypto Pay active invoice ID mismatch")
                else:
                    raise CryptoPayError("Crypto Pay returned an unknown invoice status")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Crypto Pay payment verification deferred (%s)",
                            type(exc).__name__)
    return completed



async def _claim_due_missing_rate_ids(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, now: datetime,
) -> tuple[UUID, ...]:
    """Retry paid crypto quotes fairly, without unbounded API or DB work.

    Reuse the existing per-invoice due timestamp. This does not change the
    original payment or its delivery/renewal state. All old missing quotes
    remain retryable, but at most three are claimed per worker tick.
    """
    async with system_project_session_scope(factory, project_id) as session:
        absent = ~select(CryptoPayPaidUsdRate.invoice_id).where(
            CryptoPayPaidUsdRate.invoice_id == CryptoPayInvoice.id,
        ).exists()
        rows = (await session.scalars(select(CryptoPayInvoice).where(
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.telegram_bot_id == bot_id,
            CryptoPayInvoice.status == "paid",
            CryptoPayInvoice.currency_type == "crypto",
            absent,
            or_(
                CryptoPayInvoice.next_check_at.is_(None),
                CryptoPayInvoice.next_check_at <= now,
            ),
        ).order_by(
            CryptoPayInvoice.next_check_at.asc().nullsfirst(),
            CryptoPayInvoice.paid_at, CryptoPayInvoice.id,
        ).limit(MAX_RATE_REPAIRS_PER_SCAN).with_for_update(
            skip_locked=True,
        ))).all()
        for invoice in rows:
            # Early gaps may be transient; long-lived missing provider
            # evidence is revisited daily without hammering Crypto Pay.
            age = now - invoice.paid_at
            interval = timedelta(hours=1) if age < timedelta(days=1) else timedelta(days=1)
            invoice.next_check_at = now + interval
        await session.flush()
        return tuple(row.id for row in rows)


async def _repair_missing_paid_rates(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    invoice_ids: tuple[UUID, ...], key: SecretStr,
    client_factory: Callable[..., CryptoPayClient],
) -> int:
    """Read only original merchant getInvoices receipts, never today's spot.

    A repair inserts missing financial evidence; it MUST NOT grant access,
    duplicate a purchase, rewrite any saved quote or change paid status.
    """
    if not invoice_ids:
        return 0
    grouped: dict[UUID, list[tuple]] = defaultdict(list)
    credentials: dict[UUID, tuple[str, bool]] = {}
    async with system_project_session_scope(factory, project_id) as session:
        for record in (await session.scalars(select(CryptoPayInvoice).where(
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.id.in_(invoice_ids),
            CryptoPayInvoice.status == "paid",
            CryptoPayInvoice.currency_type == "crypto",
        ))).all():
            if record.provider_invoice_id is None:
                continue
            try:
                if record.app_id not in credentials:
                    app, token = await load_crypto_app_token(
                        session, project_id=project_id, app_id=record.app_id,
                        encryption_key=key,
                    )
                    if app.telegram_bot_id != record.telegram_bot_id:
                        raise CryptoPayError("Original merchant app/bot mismatch")
                    credentials[record.app_id] = (token, app.environment == "testnet")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Crypto Pay quote credentials unavailable (%s)",
                            type(exc).__name__)
                continue
            grouped[record.app_id].append((
                record.id, record.provider_invoice_id, record.payload,
                record.amount, record.currency_code, record.currency_type,
            ))
    repaired = 0
    for app_id, rows in grouped.items():
        token, testnet = credentials[app_id]
        try:
            async with client_factory(token, testnet=testnet) as provider:
                data = await provider.get_invoices(tuple(row[1] for row in rows))
            by_id = {item["invoice_id"]: item for item in data}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Crypto Pay quote lookup deferred (%s)", type(exc).__name__)
            continue
        for invoice_id, provider_id, payload, amount, code, currency_type in rows:
            remote = by_id.get(provider_id)
            if remote is None or remote.get("status") != "paid":
                continue
            try:
                receipt = verify_paid_invoice(
                    remote, invoice_id=provider_id, payload=payload,
                    amount=amount, currency_code=code, currency_type=currency_type,
                )
                if receipt.paid_usd_rate is None:
                    continue
                async with system_project_session_scope(factory, project_id) as session:
                    await _apply_verified_receipt(
                        session, project_id=project_id,
                        invoice_id=invoice_id, receipt=receipt,
                    )
                repaired += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Crypto Pay quote repair deferred (%s)",
                            type(exc).__name__)
    return repaired


async def _deliver_one_file(
    factory: async_sessionmaker[AsyncSession], *, bot: Bot,
    project_id: UUID, now: datetime,
) -> bool:
    """One durable at-least-once Telegram delivery attempt.

    The original paid invoice is the purchase. Failed/partial deliveries
    retry after a persisted lease; /myfiles always allows manual recovery.
    """
    async with system_project_session_scope(factory, project_id) as session:
        order = await session.scalar(select(CryptoPayInvoice).where(
            CryptoPayInvoice.project_id == project_id,
            CryptoPayInvoice.telegram_bot_id == bot.id,
            CryptoPayInvoice.kind == "file",
            CryptoPayInvoice.status == "paid",
            CryptoPayInvoice.last_delivered_at.is_(None),
            or_(
                CryptoPayInvoice.next_delivery_at.is_(None),
                CryptoPayInvoice.next_delivery_at <= now,
            ),
        ).order_by(
            CryptoPayInvoice.next_delivery_at.asc().nullsfirst(),
            CryptoPayInvoice.paid_at, CryptoPayInvoice.id,
        ).limit(1).with_for_update(skip_locked=True))
        if order is None:
            return False
        # Mark the retry window *before* Telegram I/O. A killed worker or
        # Telegram 429 cannot immediately spam the buyer with duplicates.
        order.delivery_attempt_count += 1
        minutes = min(60, 2 ** min(order.delivery_attempt_count, 6))
        order.next_delivery_at = now + timedelta(minutes=minutes)
        uid, invoice_id = order.telegram_user_id, order.id
        await session.flush()
    try:
        purchase, assets = await purchased_assets(
            factory, project_id=project_id, bot_id=bot.id,
            buyer_id=uid, purchase_id=invoice_id,
        )
        # Reuse the exact existing user-facing delivery buttons and HTML;
        # import lazily to avoid the customer-runtime/router import cycle.
        from telegram_membership_saas.modules.file_sales.handlers import _deliver

        async with recipient_language(factory, uid):
            await _deliver(
                bot, user_id=uid, assets=assets,
                title=purchase.product_name, purchase_id=purchase.id,
            )
        await mark_file_delivery(
            factory, project_id=project_id, bot_id=bot.id,
            buyer_id=uid, purchase_id=invoice_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Crypto Pay file delivery deferred (%s)", type(exc).__name__)
    return True


async def reconcile_crypto_pay_once(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot: Bot, encryption_key: SecretStr | None,
    now: datetime | None = None,
    client_factory: Callable[..., CryptoPayClient] = CryptoPayClient,
) -> bool:
    """Perform bounded polling and at most one file push for one bot."""
    if encryption_key is None:
        return False
    moment = _require_utc(now or datetime.now(UTC))
    issuing, issued = await _claim_due_invoice_ids(
        factory, project_id=project_id, bot_id=bot.id, now=moment,
    )
    progress = False
    # One uncertain createInvoice lookup per worker tick. No blind reissue.
    if issuing:
        try:
            result = await recover_uncertain_crypto_invoice(
                factory, project_id=project_id, invoice_id=issuing[0],
                encryption_key=encryption_key, client_factory=client_factory,
                max_pages=ISSUING_LIST_PAGES,
            )
            if result is not None:
                # The uncertain invoice may ALREADY be paid. Verify its
                # getInvoices receipt immediately rather than delaying
                # access until the next 20-minute uncertainty window.
                issued = (*issued, result.id)
                progress = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Crypto Pay uncertain invoice deferred (%s)", type(exc).__name__)
    try:
        confirmed = await _verify_issued_batch(
            factory, project_id=project_id, invoice_ids=issued,
            key=encryption_key, client_factory=client_factory,
        )
        progress = progress or confirmed > 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Crypto Pay batch verification deferred (%s)",
                    type(exc).__name__)
    # Already-paid crypto invoices can lack a provider USD quotation.
    # Claim a separate bounded retry batch, then verify the ORIGINAL app.
    # A failed quote cannot prevent the buyer's already-paid file delivery.
    try:
        rate_ids = await _claim_due_missing_rate_ids(
            factory, project_id=project_id, bot_id=bot.id, now=moment,
        )
        repaired = await _repair_missing_paid_rates(
            factory, project_id=project_id, invoice_ids=rate_ids,
            key=encryption_key, client_factory=client_factory,
        )
        progress = progress or repaired > 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Crypto Pay missing-quote repair deferred (%s)",
                    type(exc).__name__)
    # Even when the provider is down, previously paid files still retry.
    delivered = await _deliver_one_file(
        factory, bot=bot, project_id=project_id, now=moment,
    )
    return progress or delivered


async def run_crypto_pay_recovery_loop(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
    bot: Bot, encryption_key: SecretStr | None,
    poll_seconds: float = POLL_SECONDS,
) -> None:
    """Run alongside Stars reconciliation under the existing supervisor."""
    if poll_seconds < 15:
        raise ValueError("Crypto Pay polling must be at least 15 seconds")
    if encryption_key is None:
        log.warning("Crypto Pay worker disabled: no encryption key configured")
        return
    # Deterministic stagger prevents 100 bots from calling Crypto Pay at once.
    await asyncio.sleep(bot.id % 17)
    while True:
        try:
            await reconcile_crypto_pay_once(
                factory, project_id=project_id, bot=bot,
                encryption_key=encryption_key,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Crypto Pay worker iteration failed (no invoice details)")
        await asyncio.sleep(poll_seconds)
