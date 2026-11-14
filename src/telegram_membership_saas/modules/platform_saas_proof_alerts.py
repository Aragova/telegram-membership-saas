"""One actionable receipt alert from the CENTRAL ViMi Pay bot.

The original receiving customer bot owns the media; this module never reads
media, decrypts tenant credentials, confirms bank money or creates UI menus.
PostgreSQL owns the durable once-per-proof outbox and delivery leases.
"""
from __future__ import annotations

import logging
from uuid import UUID

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.localization import recipient_language, tr
from telegram_membership_saas.tenant_context import TenantContext

log = logging.getLogger(__name__)


async def _claim(
    factory: async_sessionmaker[AsyncSession], *, platform_actor_id: UUID,
) -> dict | None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=platform_actor_id),
    ) as session:
        item = (await session.execute(text(
            "SELECT public.platform_saas_proof_alert_claim() AS data"
        ).columns(data=JSONB))).scalar_one()
    if item is not None and (
        not isinstance(item, dict)
        or any(k not in item for k in (
            "id", "generation", "amount", "currency", "pending_count",
        ))
    ):
        raise RuntimeError("Invalid platform receipt notification claim")
    return item


async def _finish(
    factory: async_sessionmaker[AsyncSession], *,
    platform_actor_id: UUID, attempt_id: UUID, generation: int,
    sent: bool,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=platform_actor_id),
    ) as session:
        await session.execute(text(
            "SELECT public.platform_saas_proof_alert_finish("
            ":id,:generation,:sent)"
        ), {"id": attempt_id, "generation": generation, "sent": sent})


def _message(item: dict) -> str:
    return (
        tr("🔔 <b>ViMi Pay · Новый чек на проверку</b>",
           "🔔 <b>ViMi Pay · New receipt for review</b>")
        + "\n\n"
        + tr("Поступил чек об оплате счёта ViMi Pay.",
             "A ViMi Pay invoice receipt was submitted.")
        + "\n💳 <b>" + str(item["amount"]) + " "
        + str(item["currency"]) + "</b>\n"
        + tr("🧾 Ожидают проверки: ", "🧾 Awaiting review: ")
        + str(item["pending_count"]) + "\n\n"
        + tr("Проверьте поступление денег в банке перед подтверждением.",
             "Verify the funds in your bank before approving.")
    )


async def deliver_one_platform_proof_alert(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, platform_actor_id: UUID, owner_telegram_id: int,
) -> bool:
    """One real Telegram incoming message, with existing proof-queue CTA.

    True means a queued item was processed, not that Telegram necessarily
    delivered it. The next central monitoring pass handles a failed retry.
    """
    item = await _claim(factory, platform_actor_id=platform_actor_id)
    if item is None:
        return False
    attempt_id = UUID(str(item["id"]))
    generation = int(item["generation"])
    sent = False
    try:
        async with recipient_language(factory, owner_telegram_id):
            await bot.send_message(
                owner_telegram_id, _message(item), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=tr("🧾 Проверка чеков", "🧾 Review receipts"),
                        callback_data="plat:v2proofs:0",
                    ),
                ]]),
            )
        sent = True
    except Exception:
        # The original receipt is durable, even when Telegram is unreachable.
        # Never log a bot token, Telegram error URL, or financial source data.
        log.warning("Central platform receipt alert delivery deferred")
    finally:
        await _finish(
            factory, platform_actor_id=platform_actor_id,
            attempt_id=attempt_id, generation=generation, sent=sent,
        )
    return True
