"""Reconcile lost successful-payment updates against Telegram's durable Stars ledger.

If the database was down just as a successful_payment update arrived, Telegram
has already debited the buyer. Never claim /myfiles can recover an unrecorded
payment unless an incoming real bot-ledger transaction has been independently
matched by invoice payload, payer ID, Stars amount and bot scope.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.file_sales.models import FilePurchase
from telegram_membership_saas.modules.file_sales.service import confirm_stars_payment

log = logging.getLogger(__name__)
PAGE_SIZE = 100
MAX_SCAN_PAGES = 20


async def recover_buyer_stars(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, project_id: UUID, buyer_id: int,
) -> tuple[UUID, ...]:
    """Persist only Telegram-confirmed payments, never trust buyer text or images.

    Called by buyer's /myfiles. The bot itself queries its own recent Stars
    ledger. If Telegram's ledger is unavailable the error is surfaced to the
    caller for clear recovery messaging, and existing paid history remains
    readable.
    """
    async with system_project_session_scope(factory, project_id) as session:
        pending = (await session.scalars(
            select(FilePurchase).where(
                FilePurchase.project_id == project_id,
                FilePurchase.telegram_bot_id == bot.id,
                FilePurchase.telegram_user_id == buyer_id,
                FilePurchase.status == "pending",
            ).order_by(FilePurchase.created_at.desc())
            .limit(100)
        )).all()
        wanted = {
            row.invoice_payload: (row.id, row.stars_amount)
            for row in pending
        }
    if not wanted:
        return ()
    matches: dict[str, tuple[str, int, datetime]] = {}
    for page in range(MAX_SCAN_PAGES):
        # offset/limit refer to *this bot's* ledger. Never use another bot's
        # credentials or trust a copied buyer receipt.
        batch = await bot.get_star_transactions(
            offset=page * PAGE_SIZE, limit=PAGE_SIZE,
        )
        transactions = batch.transactions
        if not transactions:
            break
        for record in transactions:
            source = getattr(record, "source", None)
            if (
                source is None
                or getattr(source, "type", None) != "user"
                or getattr(source, "transaction_type", None) != "invoice_payment"
                or getattr(source, "user", None) is None
                or source.user.id != buyer_id
                or record.amount <= 0
            ):
                continue
            payload = getattr(source, "invoice_payload", None)
            if payload not in wanted:
                continue
            purchase_id, stars = wanted[payload]
            if record.amount != stars or not record.id:
                log.warning(
                    "Rejected Stars reconciliation mismatch for purchase %s",
                    purchase_id,
                )
                continue
            # StarTransaction.date is the authenticated Unix event time.
            # Never substitute the later /myfiles recovery time.
            if not isinstance(getattr(record, "date", None), int):
                log.warning(
                    "Rejected Stars reconciliation without verified timestamp"
                )
                continue
            matched_at = datetime.fromtimestamp(record.date, tz=UTC)
            matches[payload] = (record.id, stars, matched_at)
        if len(matches) == len(wanted) or len(transactions) < PAGE_SIZE:
            break
    recovered = []
    for payload, (charge_id, amount, paid_at) in matches.items():
        purchase = await confirm_stars_payment(
            factory, project_id=project_id, bot_id=bot.id,
            buyer_id=buyer_id, invoice_payload=payload,
            amount=amount, currency="XTR", charge_id=charge_id,
            payment_at=paid_at,
        )
        recovered.append(purchase.id)
    return tuple(recovered)
