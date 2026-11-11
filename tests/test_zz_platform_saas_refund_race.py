"""Two-connection PostgreSQL regression for invoice void / commission credit races.

This integration case COMMITs its disposable setup solely on GitHub Actions'
ephemeral PostgreSQL service. Never run it against a reusable or live DB.
All routine SaaS tests remain rollback-only. No billing flags are changed.
"""
import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    os.getenv("GITHUB_ACTIONS") != "true"
    or not os.getenv("TMS_DATABASE_URL")
    or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Two-connection committed fixture requires disposable GitHub Actions PostgreSQL",
)


async def _actor(conn, owner):
    await conn.execute(
        text("SELECT set_config('app.current_user_id',:owner,true)"),
        {"owner": str(owner)},
    )


async def _credit(conn, line, reference):
    return (await conn.execute(text(
        "SELECT public.platform_saas_record_refund_credit("
        ":line,:reference,2,'refund')"
    ), {"line": line, "reference": reference})).scalar_one()


async def _void(conn, invoice):
    await conn.execute(
        text("UPDATE platform_saas_invoices SET status='void' WHERE id=:id"),
        {"id": invoice},
    )


async def test_refund_and_void_serialize_on_same_parent_invoice():
    """A credit/void pair cannot both commit in either arrival order."""
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(cfg.migration_database_url)
    try:
        operator_tg = _tid()
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        owner, merchant, project, bot, _ = await _project(factory, _tid())

        # These test-only records must be committed to become visible to
        # independent PostgreSQL connections. The CI database is discarded
        # immediately after the job; this case never runs on staging.
        async with admin_engine.begin() as setup:
            await setup.execute(text(
                "INSERT INTO platform_operators(telegram_user_id,role)"
                " VALUES(:tg,'owner')"
            ), {"tg": operator_tg})
            connected = (await setup.execute(text(
                "SELECT count(*) FROM platform_usage_events"
                " WHERE project_id=:project AND event_type='bot_connected'"
                " AND source_id=:bot"
            ), {"project": project, "bot": bot})).scalar_one()
            now = datetime.now(UTC)
            if not connected:
                await setup.execute(text(
                    "INSERT INTO platform_usage_events "
                    "(project_id,event_type,source_id,bot_username,"
                    "event_key,occurred_at) VALUES "
                    "(:project,'bot_connected',:bot,'credit_race_bot',"
                    ":key,:happened)"
                ), {
                    "project": project, "bot": bot,
                    "key": "credit-race-" + str(uuid4()),
                    "happened": now,
                })
            bot_tg = (await setup.execute(text(
                "SELECT telegram_bot_id FROM bot_instances WHERE id=:id"
            ), {"id": bot})).scalar_one()
            folder, product = uuid4(), uuid4()
            await setup.execute(text(
                "INSERT INTO file_sale_folders(id,project_id,name,status)"
                " VALUES(:id,:project,'Credit race','active')"
            ), {"id": folder, "project": project})
            await setup.execute(text(
                "INSERT INTO file_sale_products "
                "(id,project_id,folder_id,telegram_bot_id,name,status,stars_price)"
                " VALUES(:id,:project,:folder,:tg,'Credit race','published',200)"
            ), {
                "id": product, "project": project, "folder": folder,
                "tg": bot_tg,
            })
            invoices = []
            lines = []
            for offset in (1, 2):
                paid_at = now + timedelta(seconds=5 + offset)
                order = uuid4()
                await setup.execute(text(
                    "INSERT INTO file_sale_manual_orders "
                    "(id,project_id,product_id,telegram_bot_id,"
                    "telegram_user_id,product_name,amount,currency_code,"
                    "payment_method_id,payment_method_name,instruction,"
                    "status,paid_at) VALUES "
                    "(:id,:project,:product,:bot,7171,'Credit race',"
                    "100,'RUB',:method,'Bank','test','approved',:paid)"
                ), {
                    "id": order, "project": project, "product": product,
                    "bot": bot_tg, "method": uuid4(), "paid": paid_at,
                })
                inv = await _invoice(
                    setup, owner, merchant, None, kind="commission", total="5",
                    period_start=now - timedelta(hours=offset),
                )
                line = uuid4()
                await setup.execute(text(
                    "INSERT INTO platform_saas_invoice_lines "
                    "(id,invoice_id,project_id,source_kind,source_id,"
                    "bot_instance_id,original_amount,original_currency,"
                    "rub_per_unit,fee_rate,charge_rub) VALUES "
                    "(:line,:invoice,:project,'file_manual',:order,"
                    ":bot,100,'RUB',1,0.05,5)"
                ), {
                    "line": line, "invoice": inv, "project": project,
                    "order": order, "bot": bot,
                })
                await setup.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued'"
                    " WHERE id=:invoice"
                ), {"invoice": inv})
                invoices.append(inv)
                lines.append(line)

        # Credit wins first: the competing void must wait on its invoice
        # lock, then reject after the credit transaction commits.
        async with admin_engine.connect() as a, admin_engine.connect() as b:
            ta, tb = await a.begin(), await b.begin()
            await _actor(a, operator)
            await _actor(b, operator)
            await _credit(a, lines[0], "race:credit-first")
            started = asyncio.Event()

            async def try_void():
                started.set()
                with pytest.raises(DBAPIError) as rejected:
                    await _void(b, invoices[0])
                return getattr(rejected.value.orig, "sqlstate", None)

            task = asyncio.create_task(try_void())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.sleep(0.15)
                assert not task.done(), "Void must wait for the credit's invoice lock"
                await ta.commit()
                assert await asyncio.wait_for(task, 5) == "42501"
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if ta.is_active:
                    await ta.rollback()
                await tb.rollback()

        # Void wins first: the competing credit must wait until the void
        # commits, then re-read the now-void invoice and fail.
        async with admin_engine.connect() as a, admin_engine.connect() as b:
            ta, tb = await a.begin(), await b.begin()
            await _actor(a, operator)
            await _actor(b, operator)
            await _void(b, invoices[1])
            started = asyncio.Event()

            async def try_credit():
                started.set()
                with pytest.raises(DBAPIError) as rejected:
                    await _credit(a, lines[1], "race:void-first")
                return getattr(rejected.value.orig, "sqlstate", None)

            task = asyncio.create_task(try_credit())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.sleep(0.15)
                assert not task.done(), "Credit must wait for the void's invoice lock"
                await tb.commit()
                assert await asyncio.wait_for(task, 5) == "42501"
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if tb.is_active:
                    await tb.rollback()
                await ta.rollback()

        async with admin_engine.connect() as verify:
            rows = (await verify.execute(text(
                "SELECT id,status FROM platform_saas_invoices "
                "WHERE id IN (:one,:two)"
            ), {"one": invoices[0], "two": invoices[1]})).all()
            statuses = {row.id: row.status for row in rows}
            assert statuses == {
                invoices[0]: "issued", invoices[1]: "void",
            }
            credits = (await verify.execute(text(
                "SELECT invoice_line_id FROM platform_saas_refund_credits"
                " WHERE invoice_line_id IN (:one,:two)"
            ), {"one": lines[0], "two": lines[1]})).scalars().all()
            assert credits == [lines[0]]
            assert (await verify.execute(text(
                "SELECT enabled FROM platform_saas_billing_control WHERE id=1"
            ))).scalar_one() is False
    finally:
        await engine.dispose()
        await admin_engine.dispose()
