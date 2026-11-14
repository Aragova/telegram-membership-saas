"""Rollback-only PostgreSQL tests for pre-release test invoice void.

No live money, Telegram messages, billing activation or persistent changes.
"""
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and membership_runtime role",
)


async def _call(conn, function, params, *, json=True):
    stmt = text(
        "SELECT public." + function + "("
        + ",".join(":" + k for k in params) + ") AS result"
    )
    if json:
        stmt = stmt.columns(result=JSONB)
    return (await conn.execute(stmt, params)).scalar_one()


async def _actor(conn, user_id):
    await conn.execute(
        text("SELECT set_config('app.current_user_id',:id,true)"),
        {"id": str(user_id)},
    )


async def test_operator_void_only_after_all_proofs_rejected_and_keeps_audit():
    engine = create_async_engine(Settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                owner_tg = _tid()
                owner, merchant, project, bot, _ = await _project(
                    factory, owner_tg,
                )
                outsider, _, _, _, _ = await _project(factory, _tid())
                operator_tg = _tid()
                operator = await ensure_telegram_actor(
                    factory, telegram_id=operator_tg,
                    username=None, first_name=None,
                )
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES (:tg,'owner')"
                ), {"tg": operator_tg})
                bot_tg = (await conn.execute(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:bot"
                ), {"bot": bot})).scalar_one()
                invoice = await _invoice(
                    conn, owner, merchant, bot,
                    period_start=datetime.now(UTC),
                )
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued'"
                    " WHERE id=:invoice"
                ), {"invoice": invoice})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _actor(conn, operator)
                method = await _call(conn, "platform_saas_catalog_create", {
                    "name": "Test method",
                    "instructions": "TEST recipient 12345678",
                    "currency": "RUB",
                }, json=False)
                await _actor(conn, owner)
                attempt = await _call(
                    conn, "platform_saas_v3_create_attempt", {
                        "invoice": invoice, "amount_rub": Decimal("150.00"),
                        "currency": "RUB", "method": method,
                    },
                )
                attempt_id = UUID(str(attempt["id"]))
                await _call(conn, "platform_saas_v2_submit_proof", {
                    "attempt": attempt_id, "bot_id": bot_tg,
                    "chat_id": owner_tg, "message_id": 550,
                    "file_id": "test-proof-123456789",
                    "file_type": "document",
                }, json=False)
                params = {
                    "invoice": invoice, "owner": owner,
                    "amount": Decimal("150.00"),
                }
                await _actor(conn, outsider)
                with pytest.raises(DBAPIError) as denied:
                    async with conn.begin_nested():
                        await _call(
                            conn, "platform_saas_void_test_prepayment", params,
                        )
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                await _actor(conn, operator)
                with pytest.raises(DBAPIError) as still_pending:
                    async with conn.begin_nested():
                        await _call(
                            conn, "platform_saas_void_test_prepayment", params,
                        )
                assert getattr(still_pending.value.orig, "sqlstate", None) == (
                    "42501"
                )
                await _call(conn, "platform_saas_v2_reject_attempt", {
                    "attempt": attempt_id, "reason": "Test receipt rejected",
                }, json=False)
                # Owner must confirm the exact frozen invoice identity.
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await _call(conn, "platform_saas_void_test_prepayment", {
                            **params, "owner": outsider,
                        })
                result = await _call(
                    conn, "platform_saas_void_test_prepayment", params,
                )
                assert result["status"] == "void"
                assert result["rejected_proofs"] == 1
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await _call(
                            conn, "platform_saas_void_test_prepayment", params,
                        )
                # Historical invoice, rejected proof and immutable audit
                # survive. The debt projection excludes void invoices.
                await conn.execute(text("SET LOCAL ROLE NONE"))
                status = (await conn.execute(text(
                    "SELECT status FROM platform_saas_invoices WHERE id=:id"
                ), {"id": invoice})).scalar_one()
                assert status == "void"
                audit = (await conn.execute(text(
                    "SELECT operator_user_id,owner_user_id,amount_rub,"
                    "rejected_proofs FROM platform_saas_test_invoice_void_audit"
                    " WHERE invoice_id=:id"
                ), {"id": invoice})).one()
                assert audit.operator_user_id == operator
                assert audit.owner_user_id == owner
                assert audit.amount_rub == Decimal("150.00")
                assert audit.rejected_proofs == 1
                assert (await conn.execute(text(
                    "SELECT count(*) FROM platform_saas_payment_proofs_v2 p"
                    " JOIN platform_saas_payment_attempts_v2 a"
                    " ON a.id=p.attempt_id WHERE a.invoice_id=:id"
                ), {"id": invoice})).scalar_one() == 1
                assert (await conn.execute(text(
                    "SELECT public.platform_saas_v2_remaining(:id)"
                ), {"id": invoice})).scalar_one() == 0
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()


async def test_live_billing_switch_blocks_test_invoice_void():
    engine = create_async_engine(Settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                owner, merchant, project, bot, _ = await _project(
                    factory, _tid(),
                )
                invoice = await _invoice(
                    conn, owner, merchant, bot,
                    period_start=datetime.now(UTC),
                )
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued'"
                    " WHERE id=:id"
                ), {"id": invoice})
                operator_tg = _tid()
                operator = await ensure_telegram_actor(
                    factory, telegram_id=operator_tg,
                    username=None, first_name=None,
                )
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES (:tg,'owner')"
                ), {"tg": operator_tg})
                await conn.execute(text(
                    "UPDATE platform_saas_billing_control"
                    " SET enabled=true,effective_from=clock_timestamp()"
                    " WHERE id=1"
                ))
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _actor(conn, operator)
                with pytest.raises(DBAPIError) as error:
                    async with conn.begin_nested():
                        await _call(
                            conn, "platform_saas_void_test_prepayment", {
                                "invoice": invoice, "owner": owner,
                                "amount": Decimal("150.00"),
                            },
                        )
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
