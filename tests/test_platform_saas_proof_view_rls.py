"""Real PostgreSQL authorization for operator-only receipt retrieval.

All bank attempts and fake receipts are inside a rolled-back transaction.
No external Telegram API, real money or commercial switches are used.
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
    reason="Requires migrated PostgreSQL with runtime RLS role",
)


async def _call(conn, function, params, *, json=True):
    stmt = text(
        "SELECT public." + function + "("
        + ",".join(":" + k for k in params) + ") AS data"
    )
    if json:
        stmt = stmt.columns(data=JSONB)
    return (await conn.execute(stmt, params)).scalar_one()


async def _identity(conn, actor):
    await conn.execute(text(
        "SELECT set_config('app.current_user_id',:who,true)"
    ), {"who": str(actor)})


async def test_receipt_file_locator_is_operator_only_and_pending_only():
    engine = create_async_engine(Settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
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
                    "INSERT INTO platform_operators(telegram_user_id,role) "
                    "VALUES (:id,'owner')"
                ), {"id": operator_tg})
                source_bot_id = (await conn.execute(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:bot"
                ), {"bot": bot})).scalar_one()
                invoice = await _invoice(
                    conn, owner, merchant, bot,
                    period_start=datetime.now(UTC),
                )
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' "
                    "WHERE id=:invoice"
                ), {"invoice": invoice})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _identity(conn, operator)
                method = await _call(conn, "platform_saas_catalog_create", {
                    "name": "ViMi bank test",
                    "instructions": "TEST recipient 12345678900",
                    "currency": "RUB",
                }, json=False)
                await _identity(conn, owner)
                attempt = await _call(conn, "platform_saas_v3_create_attempt", {
                    "invoice": invoice, "amount_rub": Decimal("150.00"),
                    "currency": "RUB", "method": method,
                })
                attempt_id = UUID(str(attempt["id"]))
                await _call(conn, "platform_saas_v2_submit_proof", {
                    "attempt": attempt_id,
                    "bot_id": source_bot_id,
                    "chat_id": owner_tg,
                    "message_id": 721,
                    "file_id": "test-original-bot-file-id",
                    "file_type": "photo",
                }, json=False)
                await _identity(conn, outsider)
                with pytest.raises(DBAPIError) as error:
                    async with conn.begin_nested():
                        await _call(conn, "platform_saas_pending_proof_source", {
                            "attempt": attempt_id,
                        })
                assert getattr(error.value.orig, "sqlstate", None) == "42501"

                await _identity(conn, operator)
                record = await _call(
                    conn, "platform_saas_pending_proof_source",
                    {"attempt": attempt_id},
                )
                assert record["source_bot"] == source_bot_id
                assert record["source_project"] == str(project)
                assert record["source_file"] == "test-original-bot-file-id"
                assert record["instructions"] == "TEST recipient 12345678900"
                assert not any("token" in key for key in record)

                # The central bot is deliberately not registered as a
                # customer BotInstance. Such proofs must remain viewable.
                await _identity(conn, owner)
                central_attempt = await _call(
                    conn, "platform_saas_v3_create_attempt", {
                        "invoice": invoice, "amount_rub": Decimal("150.00"),
                        "currency": "RUB", "method": method,
                    },
                )
                central_attempt_id = UUID(str(central_attempt["id"]))
                central_tg_bot_id = _tid()
                await _call(conn, "platform_saas_v2_submit_proof", {
                    "attempt": central_attempt_id,
                    "bot_id": central_tg_bot_id,
                    "chat_id": owner_tg, "message_id": 722,
                    "file_id": "test-central-bot-file-id",
                    "file_type": "document",
                }, json=False)
                await _identity(conn, operator)
                central_record = await _call(
                    conn, "platform_saas_pending_proof_source",
                    {"attempt": central_attempt_id},
                )
                assert central_record["source_bot"] == central_tg_bot_id
                assert central_record["source_project"] is None

                await _call(conn, "platform_saas_v2_reject_attempt", {
                    "attempt": attempt_id, "reason": "Test bank funds not received",
                }, json=False)
                assert await _call(
                    conn, "platform_saas_pending_proof_source",
                    {"attempt": attempt_id},
                ) is None
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()
