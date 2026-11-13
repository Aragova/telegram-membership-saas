"""Rollback-only live-schema checks for the first rollout billing transition.

No test Telegram controls, no production invoices, and no permanent changes.
The privileged reconciliation under test is the exact migration 0077 function.
"""
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL with a rollback-only DBA transaction",
)


async def test_first_rollout_voids_only_uncollected_legacy_bills():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin = create_async_engine(settings.migration_database_url)
    try:
        old_tg = _tid()
        old_owner, old_merchant, old_project, old_bot, _ = await _project(
            factory, old_tg,
        )
        # This time originates from the same trusted PostgreSQL DB clock
        # that stamped the already-connected bot's immutable creation.
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                activated = (await conn.execute(
                    text("SELECT clock_timestamp()")
                )).scalar_one()
                old_trial = activated - timedelta(hours=72)
                old_end = activated + timedelta(hours=72)
                await conn.execute(text(
                    "INSERT INTO platform_saas_commercial_enrollments"
                    " (owner_user_id,effective_from) VALUES(:owner,:active)"
                ), {"owner": old_owner, "active": activated})
                await conn.execute(text(
                    "INSERT INTO merchant_platform_trial_claims"
                    " (id,merchant_id,owner_user_id,policy_key,scope,claimed_at)"
                    " VALUES(:id,:merchant,:owner,'vimi-saas-72h-v1','owner',:at)"
                ), {"id": uuid4(), "merchant": old_merchant,
                    "owner": old_owner, "at": old_trial})

                assert (await conn.execute(text(
                    "SELECT public.platform_saas_is_transition_owner(:owner)"
                ), {"owner": old_owner})).scalar_one() is True
                old_eligible = (await conn.execute(text(
                    "SELECT public.platform_saas_billable_after(:owner)"
                ), {"owner": old_owner})).scalar_one()
                assert old_eligible == old_end

                # Two historic-bot service invoices, one with a bank
                # attempt: only the completely uncollected one is safe to void.
                old_open = await _invoice(
                    conn, old_owner, old_merchant, old_bot,
                    period_start=activated + timedelta(minutes=1),
                )
                await _line(conn, old_open, old_project, "bot_fee", old_bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
                ), {"id": old_open})

                old_quoted = await _invoice(
                    conn, old_owner, old_merchant, old_bot,
                    period_start=activated + timedelta(minutes=2),
                )
                await _line(conn, old_quoted, old_project, "bot_fee", old_bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
                ), {"id": old_quoted})
                await conn.execute(text(
                    "INSERT INTO platform_saas_payment_attempts_v2"
                    " (id,invoice_id,owner_user_id,amount_rub,currency_code,"
                    " rub_per_unit,amount_currency,quote_date,"
                    " instructions_snapshot,expires_at)"
                    " VALUES(:id,:invoice,:owner,150,'RUB',1,150,:date,"
                    " 'Original bank payment method',:expires)"
                ), {"id": uuid4(), "invoice": old_quoted,
                    "owner": old_owner,
                    "date": activated.date(),
                    "expires": activated+timedelta(days=2)})

                # New account connected AFTER the first commercial rollout.
                # Its upcoming due-at-trial-end prepayment is legitimate.
                new_owner, new_merchant, new_project, new_bot, _ = (
                    await _project(factory, _tid())
                )
                new_claim = (await conn.execute(
                    text("SELECT clock_timestamp()")
                )).scalar_one()
                await conn.execute(text(
                    "INSERT INTO platform_saas_commercial_enrollments"
                    " (owner_user_id,effective_from) VALUES(:owner,:active)"
                ), {"owner": new_owner, "active": activated})
                await conn.execute(text(
                    "INSERT INTO merchant_platform_trial_claims"
                    " (id,merchant_id,owner_user_id,policy_key,scope,claimed_at)"
                    " VALUES(:id,:merchant,:owner,'vimi-saas-72h-v1','owner',:at)"
                ), {"id": uuid4(), "merchant": new_merchant,
                    "owner": new_owner, "at": new_claim})
                assert (await conn.execute(text(
                    "SELECT public.platform_saas_is_transition_owner(:owner)"
                ), {"owner": new_owner})).scalar_one() is False
                assert (await conn.execute(text(
                    "SELECT public.platform_saas_billable_after(:owner)"
                ), {"owner": new_owner})).scalar_one() == (
                    new_claim + timedelta(hours=72)
                )
                new_future = await _invoice(
                    conn, new_owner, new_merchant, new_bot,
                    period_start=new_claim+timedelta(hours=72),
                )
                await _line(conn, new_future, new_project, "bot_fee", new_bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
                ), {"id": new_future})

                result = (await conn.execute(text(
                    "SELECT public.platform_saas_reconcile_first_rollout_invoices()"
                    " AS result"
                ).columns(result=JSONB))).scalar_one()
                assert result["voided"] == 1
                assert result["skipped"] == 1
                states = (await conn.execute(text(
                    "SELECT id,status FROM platform_saas_invoices "
                    "WHERE id IN (:open,:quoted,:future)"
                ), {"open": old_open, "quoted": old_quoted,
                    "future": new_future})).all()
                assert dict(states) == {
                    old_open: "void",
                    old_quoted: "issued",
                    new_future: "issued",
                }
                audit_id = (await conn.execute(text(
                    "SELECT invoice_id FROM "
                    "platform_saas_trial_transition_void_audit "
                    "WHERE invoice_id=:id"
                ), {"id": old_open})).scalar_one()
                assert audit_id == old_open
                repeat = (await conn.execute(text(
                    "SELECT public.platform_saas_reconcile_first_rollout_invoices()"
                    " AS result"
                ).columns(result=JSONB))).scalar_one()
                assert repeat == {"voided": 0, "skipped": 1}
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()
