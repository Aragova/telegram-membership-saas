"""Idempotent file Stars refund recording from authenticated Telegram updates.

No buyer-submitted text, photo, receipt or callback can create a refund.
The bot-specific Telegram refunded_payment service message is the only writer.
The original paid file order and its delivery history remain intact.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.file_sales.models import (
    FilePurchase,
    FileStarRefund,
)


async def record_telegram_file_star_refund(
    factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    bot_id: int,
    buyer_id: int,
    invoice_payload: str,
    charge_id: str,
    amount: int,
    currency: str,
    refunded_at: datetime,
) -> UUID:
    """Persist one full refund if it matches the exact paid Stars charge.

    Repeated authenticated updates are harmless. Partial/mismatched amounts
    fail closed until Telegram's partial-refund semantics are supported.
    """
    if (
        currency != "XTR"
        or amount <= 0
        or not invoice_payload.startswith("fs:")
        or not charge_id
        or len(charge_id) > 256
        or refunded_at.tzinfo is None
    ):
        raise ValueError("Invalid Telegram Stars refund service event")
    moment = refunded_at.astimezone(UTC)
    if moment > datetime.now(UTC).replace(microsecond=999999):
        # Telegram-authenticated message date may precede ingestion but not
        # originate in the future. A clock skew can be retried safely.
        raise ValueError("Future-dated Telegram Stars refund")
    async with system_project_session_scope(factory, project_id) as session:
        purchase = await session.scalar(
            select(FilePurchase).where(
                FilePurchase.project_id == project_id,
                FilePurchase.telegram_bot_id == bot_id,
                FilePurchase.telegram_user_id == buyer_id,
                FilePurchase.invoice_payload == invoice_payload,
            ).with_for_update()
        )
        if (
            purchase is None
            or purchase.status != "paid"
            or purchase.telegram_charge_id != charge_id
            or purchase.stars_amount != amount
            or purchase.paid_at is None
            or moment < purchase.paid_at
        ):
            raise PermissionError("Refund does not match a verified file payment")
        existing = await session.scalar(select(FileStarRefund).where(
            FileStarRefund.purchase_id == purchase.id,
        ))
        if existing is not None:
            if (
                existing.telegram_charge_id != charge_id
                or existing.stars_amount != amount
                or existing.telegram_bot_id != bot_id
                or existing.telegram_user_id != buyer_id
                or existing.invoice_payload != invoice_payload
            ):
                raise PermissionError("Conflicting refund for this paid purchase")
            return existing.id
        refund = FileStarRefund(
            project_id=project_id,
            purchase_id=purchase.id,
            telegram_bot_id=bot_id,
            telegram_user_id=buyer_id,
            telegram_charge_id=charge_id,
            invoice_payload=invoice_payload,
            stars_amount=amount,
            refunded_at=moment,
        )
        session.add(refund)
        await session.flush()
        return refund.id
