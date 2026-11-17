"""Bounded provider GET reconciliation in the EXISTING central bot process.

Provider-created invoices are paid only after authoritative getInvoices/GET
and atomic original-invoice SQL accounting. No second Telegram poller,
no merchant app credentials and no paid state inferred from any URL.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.localization import tr
from telegram_membership_saas.modules.platform_saas_gateway_service import (
    list_platform_pending_gateway_orders,
    verify_and_record_platform_gateway_payment,
)

log = logging.getLogger(__name__)
RECOVERY_INTERVAL_SECONDS = 60


async def recover_platform_gateway_once(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, settings: Settings,
) -> int:
    if settings.platform_owner_telegram_id is None:
        return 0
    enabled_crypto = settings.saas_crypto_pay_api_token is not None
    enabled_lava = (
        settings.saas_lava_top_api_key is not None
        and settings.saas_lava_top_offer_id is not None
    )
    if not (enabled_crypto or enabled_lava):
        return 0

    orders = await list_platform_pending_gateway_orders(
        factory, settings=settings, limit=100,
    )
    applied = 0
    for order in orders:
        provider = order.get("provider")
        if provider == "crypto" and not enabled_crypto:
            continue
        if provider == "lava" and not enabled_lava:
            continue
        try:
            outcome = await verify_and_record_platform_gateway_payment(
                factory, settings=settings, pending=order,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not advance a durable cursor or claim success. This order
            # remains uncharged and will be retried on the next scan.
            log.warning(
                "Platform %s invoice provider verification unavailable",
                provider,
            )
            continue
        if outcome is None or outcome.get("duplicate"):
            continue
        if outcome["applied"]:
            applied += 1
            try:
                await bot.send_message(
                    int(order["payer_telegram_id"]),
                    tr(
                        "✅ Счёт ViMi Pay оплачен. Платёж проверен, "
                        "обслуживание бота продлено.",
                        "✅ ViMi Pay invoice paid. The provider confirmed "
                        "your payment and service was renewed.",
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Could not notify platform customer of confirmed payment")
        elif outcome.get("requires_review"):
            try:
                await bot.send_message(
                    settings.platform_owner_telegram_id,
                    tr(
                        "⚠️ Оплата внешнего провайдера зарегистрирована, "
                        "но счёт уже изменился. Проверьте финансовый журнал.",
                        "⚠️ An external provider payment was recorded, "
                        "but the invoice changed. Review the financial ledger.",
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Could not notify platform owner about unapplied payment")
    return applied


async def run_platform_gateway_recovery_loop(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, settings: Settings,
) -> None:
    """Exactly one bounded scanner task, sharing the central bot token."""
    while True:
        try:
            await recover_platform_gateway_once(
                factory, bot=bot, settings=settings,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Central platform external gateway reconciliation failed")
        await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
