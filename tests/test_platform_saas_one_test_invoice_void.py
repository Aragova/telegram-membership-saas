"""A voided test SaaS invoice cannot receive new proofs or collections.

Run against migrated PostgreSQL; all inserted records live inside a
rollback-only transaction. The one-off production migration only targets
the exact, owner-authorized November 13 invoice.
"""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL, runtime RLS and rollback-only context",
)


async def test_voided_invoice_rejects_proof_for_existing_test_quote():
    cfg = Settings()
    admin = create_async_engine(cfg.migration_database_url)
    try:
        tg_id = _tid()
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                # Every helper session joins a SAVEPOINT of the same
                # outer transaction, so even registered bot + channel
                # fixtures disappear with tx.rollback().
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                owner, merchant, project, bot, _ = await _project(factory, tg_id)
                now = datetime.now(UTC)
                invoice = await _invoice(
                    conn, owner, merchant, bot,
                    period_start=now-timedelta(hours=3),
                )
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued'"
                    " WHERE id=:invoice"
                ), {"invoice": invoice})
                attempt = uuid4()
                await conn.execute(text(
                    "INSERT INTO platform_saas_payment_attempts_v2 "
                    "(id,invoice_id,owner_user_id,amount_rub,currency_code,"
                    "rub_per_unit,amount_currency,quote_date,"
                    "instructions_snapshot,expires_at) "
                    "VALUES(:attempt,:invoice,:owner,150,'RUB',1,150,:day,"
                    "'Test payment instructions',:expires)"
                ), {
                    "attempt": attempt, "invoice": invoice, "owner": owner,
                    "day": now.date(), "expires": now+timedelta(hours=1),
                })
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='void'"
                    " WHERE id=:invoice"
                ), {"invoice": invoice})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:owner,true)"
                ), {"owner": str(owner)})
                with pytest.raises(DBAPIError) as error:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_v2_submit_proof("
                            ":attempt,:bot,:chat,:message,:file,:kind)"
                        ), {
                            "attempt": attempt, "bot": 123987123,
                            "chat": tg_id, "message": 997,
                            "file": "test-document-file-id-12345",
                            "kind": "document",
                        })
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
                await conn.execute(text("RESET ROLE"))
                assert (await conn.execute(text(
                    "SELECT status FROM platform_saas_invoices WHERE id=:id"
                ), {"id": invoice})).scalar_one() == "void"
                assert (await conn.execute(text(
                    "SELECT count(*) FROM platform_saas_payment_attempts_v2 "
                    "WHERE invoice_id=:id"
                ), {"id": invoice})).scalar_one() == 1
                assert (await conn.execute(text(
                    "SELECT count(*) FROM platform_saas_payment_proofs_v2 "
                    "WHERE attempt_id=:attempt"
                ), {"attempt": attempt})).scalar_one() == 0
                assert (await conn.execute(text(
                    "SELECT public.platform_saas_v2_remaining(:id)"
                ), {"id": invoice})).scalar_one() == 0
            finally:
                await tx.rollback()
        # Fail CI if any helper accidentally committed user/bot fixtures.
        async with admin.connect() as verify:
            assert (await verify.execute(text(
                "SELECT count(*) FROM telegram_users WHERE id=:owner"
            ), {"owner": owner})).scalar_one() == 0
            assert (await verify.execute(text(
                "SELECT count(*) FROM bot_instances WHERE id=:bot"
            ), {"bot": bot})).scalar_one() == 0
    finally:
        await admin.dispose()
