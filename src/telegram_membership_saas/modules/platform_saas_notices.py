"""Durable ViMi Pay platform invoice / due / overdue Telegram reminders.

Runs in the already existing central platform-monitor loop. The PostgreSQL
owner-only RPCs own queue visibility and status transitions; no cross-tenant
model read, new supervisor, or Telegram send inside a DB transaction.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape
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


async def _rpc(factory, actor: UUID, sql: str, params: dict | None = None):
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor),
    ) as session:
        return (await session.execute(
            text(sql).columns(data=JSONB), params or {},
        )).scalar_one()


async def schedule_saas_reminders(
    factory: async_sessionmaker[AsyncSession], *,
    platform_actor_id: UUID,
) -> int:
    # The SQL function checks the provisioned operator on each invocation.
    async with tenant_session_scope(
        factory, TenantContext(user_id=platform_actor_id),
    ) as session:
        return int((await session.execute(text(
            "SELECT public.platform_saas_notice_schedule()"
        ))).scalar_one())


def _notice_text(item: dict) -> str:
    kind = item["kind"]
    title = {
        "issued": tr("🧾 Счёт ViMi Pay", "🧾 ViMi Pay invoice issued"),
        "due": tr("⏳ Напоминание об оплате", "⏳ Payment reminder"),
        "overdue": tr("⚠️ Просрочен счёт ViMi Pay", "⚠️ ViMi Pay invoice overdue"),
    }[kind]
    deadline = datetime.fromisoformat(
        str(item["due_at"]).replace("Z", "+00:00")
    ).astimezone(UTC).strftime("%d.%m.%Y")
    # The operator may later disable or rotate bank details. Never
    # embed an old payment destination in an immutable reminder outbox.
    return (
        "<b>" + title + "</b>\n\n"
        + tr("Номер счёта: ", "Invoice ID: ")
        + "<code>" + escape(str(item["invoice_id"])) + "</code>\n"
        + tr("Сумма: ", "Amount: ")
        + "<b>" + escape(str(item["amount_rub"])) + " ₽</b>\n"
        + tr("Срок оплаты: ", "Due date: ") + deadline + "\n\n"
        + tr(
            "Откройте «🧾 Мои счета», выберите актуальную валюту и "
            "реквизиты ViMi Pay, затем отправьте чек. "
            "Оплата подтверждается после проверки поступления. "
            "Уже купленные подписки сохраняются.",
            "Open «🧾 My invoices» to choose the current ViMi Pay "
            "currency and payment instructions, then upload your receipt. "
            "Payment is confirmed after bank verification. "
            "Existing paid subscriptions remain valid.",
        )
    )


async def deliver_one_saas_notice(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot,
    platform_actor_id: UUID,
) -> bool:
    item = await _rpc(
        factory, platform_actor_id,
        "SELECT public.platform_saas_notice_claim() AS data",
    )
    if item is None:
        return False
    ident = UUID(str(item["id"]))
    attempt = int(item["attempt"])
    sent = False
    try:
        recipient = int(item["recipient"])
        async with recipient_language(factory, recipient):
            await bot.send_message(
                recipient, _notice_text(item), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=tr("🧾 Мои счета", "🧾 My invoices"),
                        callback_data="own:saasinvoices:0",
                    ),
                ]]),
            )
        sent = True
    except Exception:
        # Never log bank instructions, recipient identity or token-bearing URLs.
        log.warning("SaaS billing notice delivery deferred")
    finally:
        async with tenant_session_scope(
            factory, TenantContext(user_id=platform_actor_id),
        ) as session:
            await session.execute(
                text(
                    "SELECT public.platform_saas_notice_finish("
                    ":id,:attempt,:sent)"
                ),
                {"id": ident, "attempt": attempt, "sent": sent},
            )
    return True
