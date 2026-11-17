"""Recover central ViMi Pay Stars from Telegram's authoritative transaction history.

Telegram's original successful_payment/refunded_payment update can be consumed
while PostgreSQL is unavailable. The central bot continuously reconciles its
provider-side, independently retained StarTransactions history instead of
trusting the one-shot update delivery or a transient in-memory retry queue.

The same immutable, payer-bound SQL RPCs as live handlers provide exact-once
allocation. No second poller, webhooks, tenant tokens or external secrets.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.modules.platform_saas_stars_service import (
    STARS_LIMIT,
    confirm_platform_stars_charge,
    record_platform_stars_refund,
    recover_platform_stars_charge_payload,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 100
PAGE_DELAY_SECONDS = 2
FULL_SCAN_DELAY_SECONDS = 60
RETRY_DELAY_SECONDS = 30


def _utc_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    raise ValueError("Invalid Telegram Stars transaction timestamp")


async def reconcile_provider_stars_transaction(
    factory: async_sessionmaker[AsyncSession], *,
    bot_id: int, transaction: Any,
) -> bool:
    """Only accept the central bot's authoritative invoice-payment evidence.

    Non-platform transfers, paid-media transactions and malformed provider
    metadata cannot create SaaS credits. Refunds lacking the optional payload
    can be recovered by an exact, already recorded provider charge ID.
    """
    charge = getattr(transaction, "id", None)
    amount = getattr(transaction, "amount", None)
    fractional = getattr(transaction, "nanostar_amount", None)
    if (
        not isinstance(charge, str) or not 8 <= len(charge) <= 255
        or not isinstance(amount, int) or isinstance(amount, bool)
        or not 1 <= amount <= STARS_LIMIT
        or fractional not in (None, 0)
    ):
        return False
    source = getattr(transaction, "source", None)
    receiver = getattr(transaction, "receiver", None)
    if (source is None) == (receiver is None):
        return False
    partner = source if source is not None else receiver
    if (
        getattr(partner, "type", None) != "user"
        or getattr(partner, "transaction_type", None) != "invoice_payment"
    ):
        return False
    user = getattr(partner, "user", None)
    payer = getattr(user, "id", None)
    if not isinstance(payer, int) or payer <= 0:
        return False
    timestamp = _utc_date(getattr(transaction, "date", None))
    payload = getattr(partner, "invoice_payload", None)
    if source is not None:
        if not isinstance(payload, str) or not payload.startswith("vps:"):
            # Some Bot API StarTransaction sources omit the optional payload.
            # Never infer an invoice from a matching amount alone.
            log.warning(
                "Provider Stars invoice has no recoverable payload; "
                "operator reconciliation required for charge %s", charge,
            )
            return False
        if amount <= 0:
            return False
        result = await confirm_platform_stars_charge(
            factory, payer_telegram_id=payer, central_bot_id=bot_id,
            payload=payload, currency="XTR", stars_amount=amount,
            charge_id=charge, paid_at=timestamp,
        )
        return not result.get("duplicate", False)
    # Bot API JSON serializes an outgoing refund as a positive amount,
    # using receiver (not source) to identify its direction. Rejecting
    # positive refunds would silently retain paid service after a refund.
    if not isinstance(payload, str) or not payload.startswith("vps:"):
        payload = await recover_platform_stars_charge_payload(
            factory, payer_telegram_id=payer,
            central_bot_id=bot_id, charge_id=charge,
        )
        if payload is None:
            log.warning(
                "Provider Stars refund has no known order yet; "
                "will retry charge %s on the next history scan", charge,
            )
            return False
    return await record_platform_stars_refund(
        factory, payer_telegram_id=payer, central_bot_id=bot_id,
        payload=payload, currency="XTR", stars_amount=amount,
        charge_id=charge, refunded_at=timestamp,
    )


async def reconcile_platform_stars_page(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, offset: int,
) -> tuple[int, bool]:
    """Return next provider offset and whether a complete scan ended.

    If a DB write fails, raise *before* advancing the offset. This page
    will be processed again and safe duplicates will be ignored in SQL.
    Each full scan starts at offset 0, covering post-crash gaps and paging
    shifts without introducing another durable cursor to lose.
    """
    if offset < 0:
        raise ValueError("Invalid provider history offset")
    result = await bot.get_star_transactions(offset=offset, limit=PAGE_SIZE)
    transactions = getattr(result, "transactions", None)
    if not isinstance(transactions, list):
        raise RuntimeError("Invalid Telegram Stars history response")
    if len(transactions) > PAGE_SIZE:
        raise RuntimeError("Telegram Stars history page exceeds limit")
    for transaction in transactions:
        await reconcile_provider_stars_transaction(
            factory, bot_id=bot.id, transaction=transaction,
        )
    return offset + len(transactions), len(transactions) < PAGE_SIZE


async def run_platform_stars_recovery_loop(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot,
) -> None:
    """Background reconciliation in the EXISTING central bot process only.

    No separate polling token, no tenant bot and no automatic refunds.
    DB/API outages retry the same history page; restart rescans from zero.
    """
    offset = 0
    while True:
        try:
            offset, complete = await reconcile_platform_stars_page(
                factory, bot=bot, offset=offset,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Central Stars history reconciliation failed at offset %d",
                offset,
            )
            await asyncio.sleep(RETRY_DELAY_SECONDS)
        else:
            if complete:
                offset = 0
                await asyncio.sleep(FULL_SCAN_DELAY_SECONDS)
            else:
                await asyncio.sleep(PAGE_DELAY_SECONDS)
