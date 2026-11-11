"""Reconcile native subscription Stars charges and refunds from Telegram.

Bot API getStarTransactions is independently authenticated by the connected
bot token. Outgoing refund transactions reuse the original charge ID,
carry a positive amount in Bot API JSON, and set receiver (not source).
An incoming transaction is not evidence that no refunds exist.
This job retries after database outages and missed Telegram service updates.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.stars_subscription_refunds import (
    record_verified_subscription_star_refund,
)
from telegram_membership_saas.modules.stars_subscription_service import (
    confirm_subscription_stars_payment,
)
from telegram_membership_saas.modules.stars_subscriptions import (
    SubscriptionStarOrder,
)

log = logging.getLogger(__name__)
PAGE_SIZE = 100
MAX_SCAN_PAGES = 20
PERIODIC_SCAN_PAGES = 5


async def reconcile_subscription_star_ledger(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, project_id: UUID, buyer_id: int | None = None,
    pages: int = PERIODIC_SCAN_PAGES,
) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]:
    """Replay verified charges and reversals in event time order.

    A returned prior period must not push a later purchase into the future.
    Exact charge ID, buyer, bot, XTR amount and optional refund payload are
    checked against project-scoped DB orders. Do not trust a screenshot,
    callback, balance total, or a loosely matched transaction.
    """
    if pages not in range(1, MAX_SCAN_PAGES + 1):
        raise ValueError("Invalid Telegram Stars reconciliation page count")
    # No Stars orders means no useful outgoing receipt can match our app;
    # skip API requests for newly connected/manual-only tenant bots.
    async with system_project_session_scope(factory, project_id) as session:
        candidate = await session.scalar(select(SubscriptionStarOrder.id).where(
            SubscriptionStarOrder.project_id == project_id,
            SubscriptionStarOrder.telegram_bot_id == bot.id,
            SubscriptionStarOrder.status.in_(("paid", "pending")),
            *(
                (SubscriptionStarOrder.telegram_user_id == buyer_id,)
                if buyer_id is not None else ()
            ),
        ).limit(1))
    if candidate is None:
        return (), ()
    incoming: dict[tuple[str, int], tuple[int, str, datetime]] = {}
    outgoing: dict[tuple[str, int], tuple[int, str | None, datetime]] = {}
    for page in range(pages):
        batch = await bot.get_star_transactions(
            offset=page * PAGE_SIZE, limit=PAGE_SIZE,
        )
        transactions = batch.transactions
        if not transactions:
            break
        for item in transactions:
            moment = getattr(item, "date", None)
            if (
                not isinstance(moment, datetime)
                or moment.tzinfo is None
                or not item.id
            ):
                continue
            at = moment.astimezone(UTC)
            source = getattr(item, "source", None)
            if (
                source is not None
                and getattr(source, "type", None) == "user"
                and getattr(source, "transaction_type", None) == "invoice_payment"
                and getattr(source, "user", None) is not None
                and item.amount > 0
            ):
                user = source.user.id
                payload = getattr(source, "invoice_payload", None)
                if (
                    (buyer_id is None or buyer_id == user)
                    and isinstance(payload, str)
                    and payload.startswith("sp:")
                ):
                    incoming[(payload, user)] = (item.amount, item.id, at)
            receiver = getattr(item, "receiver", None)
            if (
                receiver is not None
                and getattr(receiver, "type", None) == "user"
                and getattr(receiver, "transaction_type", None) == "invoice_payment"
                and getattr(receiver, "user", None) is not None
                and item.amount > 0
                and source is None
                and (buyer_id is None or buyer_id == receiver.user.id)
            ):
                # The Bot API serializes outgoing refunds with a positive
                # Stars amount and sets receiver (not source). It reverses
                # the signed TDLib amount when constructing JSON. Match
                # the original charge by ID and exact unsigned amount.
                payload = getattr(receiver, "invoice_payload", None)
                if payload is None or isinstance(payload, str):
                    outgoing[(item.id, receiver.user.id)] = (
                        item.amount, payload, at,
                    )
        if len(transactions) < PAGE_SIZE:
            break

    # Telegram lists newest first, but fulfilling all positive charges
    # before refunds is WRONG when a refunded A preceded a paid B.
    # Replay chronologically and keep the positive half of a same-second
    # charge before its refund. With equal Unix seconds, resolve paired
    # charge/refunds before unrelated new purchases to avoid gaps.
    refund_keys = set(outgoing)
    events: list[tuple[datetime, int, str, tuple]] = []
    for (payload, user), (amount, charge_id, at) in incoming.items():
        priority = 0 if (charge_id, user) in refund_keys else 2
        events.append((at, priority, "paid", (payload, user, amount, charge_id)))
    for (charge_id, user), (amount, payload, at) in outgoing.items():
        events.append((at, 1, "refund", (charge_id, user, amount, payload)))
    events.sort(key=lambda event: (event[0], event[1], str(event[3])))

    payments: list[UUID] = []
    refunds: list[UUID] = []
    for at, _, kind, entry in events:
        if kind == "paid":
            payload, user, amount, charge_id = entry
            try:
                async with system_project_session_scope(
                    factory, project_id,
                ) as session:
                    order, _ = await confirm_subscription_stars_payment(
                        session, project_id=project_id, bot_id=bot.id,
                        buyer_id=user, payload=payload, currency="XTR",
                        amount=amount, charge_id=charge_id, paid_at=at,
                    )
                    # Historical paid evidence must be saved even after
                    # the owner ended the subscription. Do not report such
                    # non-entitling charges as recovered customer access.
                    usable_grant = await session.scalar(
                        select(EntitlementGrant.id).where(
                            EntitlementGrant.project_id == project_id,
                            EntitlementGrant.source == EntitlementSource.STARS_PAYMENT.value,
                            EntitlementGrant.source_id == order.id,
                            EntitlementGrant.status == EntitlementGrantStatus.ACTIVE.value,
                        ).limit(1)
                    )
                    if usable_grant is not None:
                        payments.append(order.id)
            except (ValueError, PermissionError):
                # Unrelated buyer/provider transactions must not block a
                # valid event in the same independently verified batch.
                log.debug("Unmatched Stars subscription charge, ignoring")
        else:
            charge_id, user, amount, payload = entry
            async with system_project_session_scope(
                factory, project_id,
            ) as session:
                order = await session.scalar(select(SubscriptionStarOrder).where(
                    SubscriptionStarOrder.project_id == project_id,
                    SubscriptionStarOrder.telegram_bot_id == bot.id,
                    SubscriptionStarOrder.telegram_user_id == user,
                    SubscriptionStarOrder.telegram_charge_id == charge_id,
                    SubscriptionStarOrder.stars_amount == amount,
                    SubscriptionStarOrder.status == "paid",
                ))
                if order is None or (
                    payload is not None and payload != order.invoice_payload
                ):
                    continue
                refund_id = await record_verified_subscription_star_refund(
                    session, project_id=project_id, bot_id=bot.id,
                    buyer_id=user, payload=order.invoice_payload,
                    amount=amount, currency="XTR", charge_id=charge_id,
                    refunded_at=at,
                )
                refunds.append(refund_id)
    return tuple(set(payments)), tuple(set(refunds))


async def recover_subscription_stars(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, project_id: UUID, buyer_id: int,
) -> tuple[UUID, ...]:
    """Buyer's recovery command checks both charges and refunded charges."""
    payments, _ = await reconcile_subscription_star_ledger(
        factory, bot=bot, project_id=project_id,
        buyer_id=buyer_id, pages=MAX_SCAN_PAGES,
    )
    return payments


async def run_subscription_stars_ledger_loop(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, project_id: UUID,
) -> None:
    """One in-process periodic recovery, not a separate supervisor/worker.

    A failed or missed refunded_payment is recovered after DB/Telegram
    connectivity returns, without waiting for the buyer to open /starsrecover.
    """
    while True:
        try:
            paid, refunds = await reconcile_subscription_star_ledger(
                factory, bot=bot, project_id=project_id,
            )
            if refunds:
                log.info(
                    "Recovered %d Stars subscription refunds for project %s",
                    len(refunds), project_id,
                )
            if paid:
                log.info(
                    "Reconciled %d Stars subscription purchases for project %s",
                    len(paid), project_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Stars ledger reconciliation unavailable for project %s",
                project_id,
            )
        # Stagger calls across up to 100 independent bots; no extra VM.
        await asyncio.sleep(900 + bot.id % 180)
