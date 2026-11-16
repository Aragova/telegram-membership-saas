"""Full database regressions: invisible invoice prompt + operator confirmation.

Real PostgreSQL schema, migrations and owner-only RLS. All test financial
records and access grants are rolled back; no live bot or banking calls.
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
from telegram_membership_saas.modules.owner_setup.service import (
    ensure_telegram_actor,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires real migrated PostgreSQL",
)


async def test_private_payment_prompt_binding_and_single_operator_click_ledger():
    engine = create_async_engine(Settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                payer_chat, operator_chat = _tid(), _tid()
                owner, merchant, project, bot, _ = await _project(
                    factory, payer_chat,
                )
                other_owner, _, _, _, _ = await _project(
                    factory, _tid(),
                )
                operator = await ensure_telegram_actor(
                    factory, telegram_id=operator_chat,
                    username=None, first_name="Cash verified",
                )
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role) "
                    "VALUES (:id,'owner')"
                ), {"id": operator_chat})
                invoice = await _invoice(conn, owner, merchant, bot)
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' "
                    "WHERE id=:id"
                ), {"id": invoice})
                attempt = uuid4()
                now = datetime.now(UTC)
                await conn.execute(text("""
                    INSERT INTO platform_saas_payment_attempts_v2
                    (id,invoice_id,owner_user_id,amount_rub,currency_code,
                     rub_per_unit,amount_currency,quote_date,
                     instructions_snapshot,expires_at)
                    VALUES(:attempt,:invoice,:owner,150,'RUB',1,150,:day,
                           'Private local bank instructions',:expiry)
                """), {
                    "attempt": attempt, "invoice": invoice, "owner": owner,
                    "day": now.date(), "expiry": now + timedelta(days=2),
                })
                bot_telegram = await conn.scalar(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:id"
                ), {"id": bot})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:user,true)"
                ), {"user": str(owner)})
                bound = await conn.scalar(text(
                    "SELECT public.platform_saas_bind_payment_prompt("
                    ":attempt,:bot,:chat,808)"
                ), {"attempt": attempt, "bot": bot_telegram,
                     "chat": payer_chat})
                assert bound is True
                found = await conn.scalar(text(
                    "SELECT public.platform_saas_resolve_payment_prompt("
                    ":bot,:chat,808)"
                ), {"bot": bot_telegram, "chat": payer_chat})
                assert found == attempt
                assert await conn.scalar(text(
                    "SELECT public.platform_saas_resolve_payment_prompt("
                    ":bot,:chat,809)"
                ), {"bot": bot_telegram, "chat": payer_chat}) is None
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.scalar(text(
                            "SELECT count(*) FROM platform_saas_payment_prompts"
                        ))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:user,true)"
                ), {"user": str(other_owner)})
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.scalar(text(
                            "SELECT public.platform_saas_resolve_payment_prompt("
                            ":bot,:chat,808)"
                        ), {"bot": bot_telegram, "chat": payer_chat})
                # Register through the ACTUAL seven-argument runtime RPC.
                # Proof rows are immutable; the original INSERT must contain
                # the canonical identity and trigger an owner-only alert.
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:user,true)"
                ), {"user": str(owner)})
                recorded = await conn.scalar(text(
                    "SELECT public.platform_saas_v2_submit_proof("
                    ":attempt,:bot,:chat,812,'unique-proof-file-test-id',"
                    "'document','canonical-proof-identity-2026')"
                ), {"attempt": attempt, "bot": bot_telegram,
                     "chat": payer_chat})
                assert recorded is not None
                await conn.execute(text("RESET ROLE"))
                stored_unique_id = await conn.scalar(text(
                    "SELECT source_file_unique_id FROM "
                    "platform_saas_payment_proofs_v2 WHERE id=:id"
                ), {"id": recorded})
                assert stored_unique_id == "canonical-proof-identity-2026"
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:user,true)"
                ), {"user": str(operator)})
                allocated = await conn.scalar(text(
                    "SELECT public.platform_saas_v2_approve_checked_attempt(:id)"
                ), {"id": attempt})
                replay = await conn.scalar(text(
                    "SELECT public.platform_saas_v2_approve_checked_attempt(:id)"
                ), {"id": attempt})
                assert allocated == replay
                await conn.execute(text("RESET ROLE"))
                row = (await conn.execute(text(
                    "SELECT bank_reference,operator_evidence_key,confirmed_by "
                    "FROM platform_saas_payment_allocations_v2 "
                    "WHERE id=:id"
                ), {"id": allocated})).one()
                assert row.bank_reference is None
                assert row.operator_evidence_key.startswith("tg-proof:")
                assert row.confirmed_by == operator

                # Same canonical Telegram file, different file_id and
                # different source bot MUST NOT settle a second invoice.
                second_invoice = await _invoice(
                    conn, owner, merchant, bot,
                    period_start=now + timedelta(days=35),
                )
                await _line(conn, second_invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' "
                    "WHERE id=:id"
                ), {"id": second_invoice})
                second_attempt = uuid4()
                await conn.execute(text("""
                    INSERT INTO platform_saas_payment_attempts_v2
                    (id,invoice_id,owner_user_id,amount_rub,currency_code,
                     rub_per_unit,amount_currency,quote_date,
                     instructions_snapshot,expires_at)
                    VALUES(:attempt,:invoice,:owner,150,'RUB',1,150,:day,
                           'Second bank payment',:expiry)
                """), {"attempt": second_attempt, "invoice": second_invoice,
                       "owner": owner, "day": now.date(),
                       "expiry": now + timedelta(days=2)})
                await conn.execute(text("""
                    INSERT INTO platform_saas_payment_proofs_v2(
                        id,attempt_id,source_bot_id,source_chat_id,
                        source_message_id,source_file_id,
                        source_file_unique_id,file_type
                    ) VALUES(
                        :id,:attempt,:bot,:chat,813,'new-rotated-file-id',
                        'canonical-proof-identity-2026','document'
                    )
                """), {
                    "id": uuid4(), "attempt": second_attempt,
                    "bot": bot_telegram + 1, "chat": payer_chat,
                })
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:user,true)"
                ), {"user": str(operator)})
                with pytest.raises(DBAPIError) as reused:
                    async with conn.begin_nested():
                        await conn.scalar(text(
                            "SELECT public.platform_saas_v2_approve_checked_attempt(:id)"
                        ), {"id": second_attempt})
                assert getattr(reused.value.orig, "sqlstate", None) == "23505"
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
