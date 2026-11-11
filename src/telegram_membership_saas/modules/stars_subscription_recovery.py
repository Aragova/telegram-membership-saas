"""Recover a Telegram-confirmed subscription Stars charge after lost updates.

Only the connected bot's authenticated StarTransaction ledger can fulfil an
unrecorded purchase, never user-submitted receipts or copied charge IDs.
"""
from datetime import UTC, datetime
from uuid import UUID

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.stars_subscription_service import (
    confirm_subscription_stars_payment,
)
from telegram_membership_saas.modules.stars_subscriptions import (
    SubscriptionStarOrder,
)

PAGE_SIZE = 100
MAX_PAGES = 20


async def recover_subscription_stars(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, project_id: UUID, buyer_id: int,
) -> tuple[UUID, ...]:
    async with system_project_session_scope(factory, project_id) as session:
        pending = (await session.scalars(
            select(SubscriptionStarOrder).where(
                SubscriptionStarOrder.project_id == project_id,
                SubscriptionStarOrder.telegram_bot_id == bot.id,
                SubscriptionStarOrder.telegram_user_id == buyer_id,
                SubscriptionStarOrder.status == "pending",
            ).order_by(SubscriptionStarOrder.created_at.desc()).limit(100)
        )).all()
        wanted = {
            order.invoice_payload: (order.id, order.stars_amount)
            for order in pending
        }
    if not wanted:
        return ()
    verified: dict[str, tuple[str, int, datetime]] = {}
    for page in range(MAX_PAGES):
        batch = await bot.get_star_transactions(
            offset=page * PAGE_SIZE, limit=PAGE_SIZE,
        )
        txs = batch.transactions
        if not txs:
            break
        for tx in txs:
            source = getattr(tx, "source", None)
            if (source is None or getattr(source, "type", None) != "user"
                or getattr(source, "transaction_type", None) != "invoice_payment"
                or getattr(source, "user", None) is None
                or source.user.id != buyer_id or not tx.id):
                continue
            payload = getattr(source, "invoice_payload", None)
            if payload not in wanted:
                continue
            order_id, amount = wanted[payload]
            if tx.amount != amount or tx.date.tzinfo is None:
                continue
            verified[payload] = (tx.id, amount, tx.date.astimezone(UTC))
        if len(verified) >= len(wanted) or len(txs) < PAGE_SIZE:
            break
    completed = []
    for payload, (charge_id, amount, moment) in verified.items():
        async with system_project_session_scope(factory, project_id) as session:
            order, _ = await confirm_subscription_stars_payment(
                session, project_id=project_id, bot_id=bot.id,
                buyer_id=buyer_id, payload=payload, currency="XTR",
                amount=amount, charge_id=charge_id, paid_at=moment,
            )
        completed.append(order.id)
    return tuple(completed)
