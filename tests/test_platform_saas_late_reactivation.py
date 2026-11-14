"""Rollback-only proof: paying a long-frozen bill restores one prepaid month.

The immutable original invoice period remains unchanged. No customer access
records, live Telegram bots, staging invoices or actual money are touched.
"""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _receipt, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires ephemeral migrated CI PostgreSQL",
)


async def test_expired_service_invoice_settlement_restores_real_coverage():
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    try:
        owner, merchant, project, bot, _ = await _project(factory, _tid())
        operator_tg = _tid()
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        now = datetime.now(UTC)
        old_start = now - timedelta(days=65)
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": operator_tg})
                await conn.execute(text(
                    "UPDATE platform_saas_billing_control SET enabled=true,"
                    "effective_from=:start WHERE id=1"
                ), {"start": now - timedelta(days=70)})
                await conn.execute(text(
                    "UPDATE platform_saas_hold_control SET enabled=true,"
                    "effective_from=:start,grace_days=0 WHERE id=1"
                ), {"start": now - timedelta(days=70)})
                invoice = await _invoice(
                    conn, owner, merchant, bot, period_start=old_start,
                )
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued'"
                    " WHERE id=:id"
                ), {"id": invoice})
                before = (await conn.execute(text(
                    "SELECT public.platform_saas_bot_hold_state("
                    ":bot,clock_timestamp())"
                ), {"bot": bot})).scalar_one()
                assert before == "held"
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(operator)})
                await _receipt(conn, invoice, operator, "150.00")
                new_period = (await conn.execute(text(
                    "SELECT a.effective_start,a.effective_end,"
                    "a.anniversary_anchor FROM "
                    "platform_saas_bot_service_activations a "
                    "WHERE a.invoice_id=:id"
                ), {"id": invoice})).one()
                assert new_period.effective_start >= now
                assert new_period.effective_end > new_period.effective_start
                assert new_period.anniversary_anchor == new_period.effective_start
                # The historical paper invoice remains immutable.
                original = (await conn.execute(text(
                    "SELECT period_start,period_end,total_rub "
                    "FROM platform_saas_invoices WHERE id=:id"
                ), {"id": invoice})).one()
                assert original.period_start == old_start
                assert original.period_end < now
                assert original.total_rub == Decimal("150.00")
                assert (await conn.execute(text(
                    "SELECT public.platform_saas_bot_hold_state("
                    ":bot,clock_timestamp())"
                ), {"bot": bot})).scalar_one() == "clear"
                # The next bill will be due at actual coverage expiry,
                # not every missed anniversary while the bot was frozen.
                next_end = (await conn.execute(text(
                    "SELECT public.platform_saas_bot_period_end(:bot,:start)"
                ), {
                    "bot": bot, "start": new_period.effective_end,
                })).scalar_one()
                assert next_end > new_period.effective_end
                assert (await conn.execute(text(
                    "SELECT public.platform_saas_v2_remaining(:invoice)"
                ), {"invoice": invoice})).scalar_one() == 0
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()
