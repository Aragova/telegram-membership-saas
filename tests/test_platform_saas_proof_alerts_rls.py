"""Real PostgreSQL proof-trigger and central-owner alert lease regression.

Every fake financial object is rolled back. No Telegram API, real bank
confirmation, live billing switch or test-only UI is involved.
"""
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid
from test_platform_saas_proof_view_rls import _call, _identity

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.owner_setup.service import (
    ensure_telegram_actor,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL with membership_runtime role",
)


@pytest.mark.asyncio
async def test_new_proof_push_once_operator_only_and_cancel_if_reviewed():
    engine = create_async_engine(Settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                merchant_tg = _tid()
                owner, merchant, project, bot, _ = await _project(
                    factory, merchant_tg,
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
                    "name": "Test bank method",
                    "instructions": "TEST BANK ACCOUNT 123456789",
                    "currency": "RUB",
                }, json=False)

                async def submit(seq):
                    await _identity(conn, owner)
                    data = await _call(
                        conn, "platform_saas_v3_create_attempt", {
                            "invoice": invoice,
                            "amount_rub": Decimal("150.00"),
                            "currency": "RUB", "method": method,
                        },
                    )
                    attempt = UUID(str(data["id"]))
                    proof = await _call(conn, "platform_saas_v2_submit_proof", {
                        "attempt": attempt, "bot_id": source_bot_id,
                        "chat_id": merchant_tg, "message_id": seq,
                        "file_id": "original-test-photo-file-id",
                        "file_type": "photo",
                    }, json=False)
                    return attempt, proof

                first, proof_id = await submit(8001)
                # Duplicate Telegram receipt delivery uses the original
                # proof, so the insert trigger cannot enqueue twice.
                await _identity(conn, owner)
                same = await _call(conn, "platform_saas_v2_submit_proof", {
                    "attempt": first, "bot_id": source_bot_id,
                    "chat_id": merchant_tg, "message_id": 8001,
                    "file_id": "original-test-photo-file-id",
                    "file_type": "photo",
                }, json=False)
                assert same == proof_id
                await _identity(conn, outsider)
                with pytest.raises(DBAPIError) as denied:
                    async with conn.begin_nested():
                        await _call(
                            conn, "platform_saas_proof_alert_claim", {},
                        )
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"

                await _identity(conn, operator)
                alert = await _call(
                    conn, "platform_saas_proof_alert_claim", {},
                )
                assert alert["id"] == str(first)
                assert alert["currency"] == "RUB"
                assert alert["generation"] == 1
                assert alert["pending_count"] >= 1
                # A live claim is leased: no second alert while sending.
                assert await _call(
                    conn, "platform_saas_proof_alert_claim", {},
                ) is None
                # Fenced finish refuses an older/incorrect claim.
                assert not await _call(conn, "platform_saas_proof_alert_finish", {
                    "id": first, "kind": "new", "generation": 0, "sent": True,
                }, json=False)
                assert await _call(conn, "platform_saas_proof_alert_finish", {
                    "id": first, "kind": "new", "generation": 1, "sent": True,
                }, json=False)
                assert await _call(
                    conn, "platform_saas_proof_alert_claim", {},
                ) is None

                second, _ = await submit(8002)
                await _identity(conn, operator)
                await _call(conn, "platform_saas_v2_reject_attempt", {
                    "attempt": second,
                    "reason": "TEST BANK FUNDS NOT RECEIVED",
                }, json=False)
                # Rejected before dispatch: no misleading phone alert.
                assert await _call(
                    conn, "platform_saas_proof_alert_claim", {},
                ) is None
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()
