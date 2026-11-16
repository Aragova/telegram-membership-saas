"""Rollback-only PostgreSQL: legacy issued bot proof grace, no retro network hold.

Exact migration 0089 functions, real schema and triggers; no Telegram traffic,
no live payments and no staging mutation.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from os import getenv
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings


@pytest.mark.skipif(
    not getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated test PostgreSQL",
)
async def test_pre_activation_bill_grace_is_bot_only_first_proof_and_not_renewable():
    admin_engine = create_async_engine(Settings().migration_database_url)
    try:
        async with admin_engine.connect() as conn:
            outer = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                payer_tg = _tid()
                owner, merchant, project, bot, _ = await _project(
                    factory, payer_tg,
                )
                # Separate owner must never inherit the first owner's debt.
                foreign_owner, _, _, foreign_bot, _ = await _project(
                    factory, _tid(),
                )
                now = datetime.now(UTC)
                invoice = await _invoice(
                    conn, owner, merchant, bot,
                    period_start=now - timedelta(days=15),
                )
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE public.platform_saas_invoices "
                    "SET status='issued' WHERE id=:id"
                ), {"id": invoice})
                issued = await conn.scalar(text(
                    "SELECT issued_at FROM public.platform_saas_invoices "
                    "WHERE id=:id"
                ), {"id": invoice})
                assert issued is not None
                # Simulate rollout after that immutable issuance timestamp.
                # ALL writes below are inside a rolled-back CI transaction.
                rollout = issued + timedelta(minutes=1)
                await conn.execute(text(
                    "UPDATE public.platform_saas_hold_control "
                    "SET enabled=true,effective_from=:at,grace_days=0 WHERE id=1"
                ), {"at": rollout})
                await conn.execute(text(
                    "UPDATE public.platform_saas_billing_control "
                    "SET enabled=true,effective_from=:at WHERE id=1"
                ), {"at": rollout})
                async def states(at):
                    own = await conn.scalar(text(
                        "SELECT public.platform_saas_owner_overdue_state(:id,:at)"
                    ), {"id": owner, "at": at})
                    bot_state = await conn.scalar(text(
                        "SELECT public.platform_saas_bot_hold_state(:id,:at)"
                    ), {"id": bot, "at": at})
                    other = await conn.scalar(text(
                        "SELECT public.platform_saas_owner_overdue_state(:id,:at)"
                    ), {"id": foreign_owner, "at": at})
                    foreign_state = await conn.scalar(text(
                        "SELECT public.platform_saas_bot_hold_state(:id,:at)"
                    ), {"id": foreign_bot, "at": at})
                    return own, bot_state, other, foreign_state

                assert (await states(rollout + timedelta(minutes=3)))[:2] == (
                    "clear", "held",
                )
                # The tenant role may read its own bot hold through the new
                # authorized project-scoped function, not query global holds.
                scope = await conn.begin_nested()
                try:
                    # The project-scoped RPC uses the real wall clock, not
                    # the simulated future timestamp of the grace test.
                    # Within this SAVEPOINT only, make the controls active
                    # already; rolling it back restores the original cutoff.
                    active = issued - timedelta(minutes=1)
                    for control in (
                        "platform_saas_hold_control",
                        "platform_saas_billing_control",
                    ):
                        await conn.execute(text(
                            "UPDATE public." + control
                            + " SET effective_from=:at WHERE id=1"
                        ), {"at": active})
                    await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                    await conn.execute(text(
                        "SELECT set_config('app.current_user_id',:actor,true)"
                    ), {"actor": str(owner)})
                    await conn.execute(text(
                        "SELECT set_config('app.current_project_id',:project,true)"
                    ), {"project": str(project)})
                    own_hold = await conn.scalar(text(
                        "SELECT public.platform_saas_project_commerce_state("
                        ":project,:bot)"
                    ), {"project": project, "bot": bot})
                    assert own_hold == "held"
                    await conn.execute(text(
                        "SELECT set_config('app.current_user_id',:actor,true)"
                    ), {"actor": str(foreign_owner)})
                    with pytest.raises(DBAPIError) as denied:
                        async with conn.begin_nested():
                            await conn.scalar(text(
                                "SELECT public.platform_saas_project_commerce_state("
                                ":project,:bot)"
                            ), {"project": project, "bot": bot})
                    assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                finally:
                    await scope.rollback()
                original = rollout + timedelta(minutes=4)
                async def proof(submitted):
                    attempt = uuid4()
                    await conn.execute(text("""
                        INSERT INTO public.platform_saas_payment_attempts_v2
                          (id,invoice_id,owner_user_id,amount_rub,
                           currency_code,rub_per_unit,amount_currency,
                           quote_date,instructions_snapshot,expires_at)
                        VALUES (:id,:invoice,:owner,:rub,'RUB',1,:rub,:day,
                                'Original bank details',:expires)
                    """), {
                        "id": attempt, "invoice": invoice, "owner": owner,
                        "rub": Decimal("150.00"), "day": now.date(),
                        "expires": original + timedelta(days=2),
                    })
                    src_bot = await conn.scalar(text(
                        "SELECT telegram_bot_id FROM public.bot_instances "
                        "WHERE id=:bot"
                    ), {"bot": bot})
                    await conn.execute(text("""
                        INSERT INTO public.platform_saas_payment_proofs_v2
                          (id,attempt_id,source_bot_id,source_chat_id,
                           source_message_id,source_file_id,file_type,
                           submitted_at)
                        VALUES (:id,:attempt,:bot,:chat,:message_id,
                                'original-test-receipt-file','document',:at)
                    """), {
                        "id": uuid4(), "attempt": attempt, "bot": src_bot,
                        "chat": payer_tg, "at": submitted,
                        "message_id": 123 if submitted == original else 124,
                    })
                    return attempt

                first = await proof(original)
                assert (await states(original + timedelta(minutes=1))) == (
                    "clear", "clear", "clear", "clear",
                )
                # A second file never resets the first immutable time.
                await proof(original + timedelta(hours=11))
                assert (await states(original + timedelta(hours=12)))[:2] == (
                    "clear", "held",
                )
                # Any decided earlier attempt removes protection even if
                # a second proof is still pending.
                await conn.execute(text("""
                    INSERT INTO public.platform_saas_payment_rejections_v2
                      (id,attempt_id,reason,rejected_by)
                    VALUES (:id,:attempt,'Bank transfer not received',:owner)
                """), {"id": uuid4(), "attempt": first, "owner": owner})
                assert (await states(original + timedelta(hours=11, minutes=1)))[:2] == (
                    "clear", "held",
                )
            finally:
                await outer.rollback()
    finally:
        await admin_engine.dispose()
