"""Central ViMi Pay method selector and immutable bank quote, rollback-only.

No real money, production receipts, external Telegram messages or bill toggles
are touched by these tests. Every fixture is inside ONE outer transaction.
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
    reason="Requires real migrated PostgreSQL transaction",
)


async def _call(conn, function, params, *, as_json=True):
    sql = text(
        "SELECT public." + function + "("
        + ",".join(":" + k for k in params) + ") AS value"
    )
    if as_json:
        sql = sql.columns(value=JSONB)
    return (await conn.execute(sql, params)).scalar_one()


async def _identity(conn, actor):
    await conn.execute(text(
        "SELECT set_config('app.current_user_id',:value,true)"
    ), {"value":str(actor)})


async def test_central_named_methods_validate_currencies_and_preserve_quotes():
    engine = create_async_engine(Settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                operator_tg = _tid()
                owner, merchant, project, bot, _ = await _project(
                    factory, _tid(),
                )
                outsider, _, _, _, _ = await _project(factory, _tid())
                operator = await ensure_telegram_actor(
                    factory, telegram_id=operator_tg,
                    username=None, first_name=None,
                )
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role) "
                    "VALUES (:id,'owner')"
                ), {"id":operator_tg})
                at = datetime.now(UTC)
                invoice = await _invoice(
                    conn, owner, merchant, bot, period_start=at,
                )
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' "
                    "WHERE id=:id"
                ), {"id":invoice})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _identity(conn, outsider)
                with pytest.raises(DBAPIError) as forbidden:
                    async with conn.begin_nested():
                        await _call(conn,"platform_saas_catalog_create",{
                            "name":"External method",
                            "instructions":"Unauthorized bank details",
                            "currency":"RUB",
                        },as_json=False)
                assert getattr(forbidden.value.orig,"sqlstate",None)=="42501"
                await _identity(conn, operator)
                method = await _call(
                    conn,"platform_saas_catalog_create",{
                        "name":"СБП ViMi Pay",
                        "instructions":"Bank reference 123456789 for test",
                        "currency":"RUB",
                    },as_json=False,
                )
                assert isinstance(method,UUID)
                assert await _call(
                    conn,"platform_saas_catalog_currency_set",{
                        "id":method,"currency":"USD","enabled":True,
                    },as_json=False,
                )
                catalog=await _call(conn,"platform_saas_catalog_read",{})
                ours=next(x for x in catalog if x["id"]==str(method))
                assert ours["currencies"]==["RUB","USD"]
                assert ours["enabled"]
                await _identity(conn, owner)
                offered=await _call(
                    conn,"platform_saas_catalog_for_invoice",
                    {"invoice":invoice},
                )
                assert any(x["id"]==str(method) and x["currency"]=="RUB"
                           for x in offered)
                assert all("instructions" not in x for x in offered)
                with pytest.raises(DBAPIError) as wrong_owner:
                    async with conn.begin_nested():
                        await _identity(conn, outsider)
                        await _call(conn,"platform_saas_catalog_for_invoice",{
                            "invoice":invoice,
                        })
                assert getattr(wrong_owner.value.orig,"sqlstate",None)=="42501"
                await _identity(conn,owner)
                attempt=await _call(
                    conn,"platform_saas_v3_create_attempt",{
                        "invoice":invoice,"amount_rub":Decimal("150.00"),
                        "currency":"RUB","method":method,
                    },
                )
                assert Decimal(attempt["amount_rub"])==Decimal("150.00")
                assert attempt["method_name"]=="СБП ViMi Pay"
                assert attempt["instructions"]=="Bank reference 123456789 for test"
                await conn.execute(text("RESET ROLE"))
                record=(await conn.execute(text(
                    "SELECT method_id,instructions_snapshot FROM "
                    "platform_saas_payment_attempts_v2 WHERE id=:id"
                ),{"id":UUID(str(attempt["id"]))})).one()
                assert record.method_id==method
                assert record.instructions_snapshot==attempt["instructions"]
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _identity(conn,operator)
                await _call(conn,"platform_saas_catalog_update",{
                    "id":method,"name":"Bank transfer",
                    "instructions":"Changed bank reference 987654321",
                },as_json=False)
                await conn.execute(text("RESET ROLE"))
                assert (await conn.execute(text(
                    "SELECT instructions_snapshot FROM "
                    "platform_saas_payment_attempts_v2 WHERE id=:id"
                ),{"id":UUID(str(attempt["id"]))})).scalar_one()==(
                    "Bank reference 123456789 for test"
                )
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _call(conn,"platform_saas_catalog_toggle",{
                    "id":method,"enabled":False,
                },as_json=False)
                await _identity(conn,owner)
                offered=await _call(conn,"platform_saas_catalog_for_invoice",{
                    "invoice":invoice,
                })
                assert not any(x["id"]==str(method) for x in offered)
                with pytest.raises(DBAPIError) as disabled:
                    async with conn.begin_nested():
                        await _call(conn,"platform_saas_v3_create_attempt",{
                            "invoice":invoice,"amount_rub":Decimal("1.00"),
                            "currency":"RUB","method":method,
                        })
                assert getattr(disabled.value.orig,"sqlstate",None)=="42501"
                await _identity(conn,operator)
                await _call(conn,"platform_saas_catalog_toggle",{
                    "id":method,"enabled":True,
                },as_json=False)
                await _call(conn,"platform_saas_catalog_currency_set",{
                    "id":method,"currency":"USD","enabled":False,
                },as_json=False)
                await _identity(conn,owner)
                with pytest.raises(DBAPIError) as detached:
                    async with conn.begin_nested():
                        await _call(conn,"platform_saas_v3_create_attempt",{
                            "invoice":invoice,"amount_rub":Decimal("1.00"),
                            "currency":"USD","method":method,
                        })
                assert getattr(detached.value.orig,"sqlstate",None)=="42501"
                # Legacy v2 RPC must not keep using bank details after
                # the matching catalog method is switched off.
                await _identity(conn,operator)
                await _call(conn,"platform_saas_v2_bank_method",{
                    "currency":"RUB",
                    "instructions":"Legacy RUB test account number 33221100",
                },as_json=False)
                legacy=await _call(conn,"platform_saas_catalog_read",{})
                legacy_method=next(m for m in legacy
                                   if m["name"]=="💳 RUB")
                await _identity(conn,owner)
                old=await _call(conn,"platform_saas_v2_create_attempt",{
                    "invoice":invoice,"amount_rub":Decimal("1.00"),
                    "currency":"RUB",
                })
                assert old["instructions"]=="Legacy RUB test account number 33221100"
                await _identity(conn,operator)
                await _call(conn,"platform_saas_catalog_toggle",{
                    "id":UUID(str(legacy_method["id"])),"enabled":False,
                },as_json=False)
                await _identity(conn,owner)
                with pytest.raises(DBAPIError) as retired:
                    async with conn.begin_nested():
                        await _call(conn,"platform_saas_v2_create_attempt",{
                            "invoice":invoice,"amount_rub":Decimal("1.00"),
                            "currency":"RUB",
                        })
                assert getattr(retired.value.orig,"sqlstate",None)=="42501"
                await _identity(conn,operator)
                # Legacy imported bank details have a stable identity even
                # after their catalog method has been renamed. Obsolete bank
                # edits update the authoritative row, never resurrect a
                # disabled named method or rewrite old payment attempts.
                await conn.execute(text("RESET ROLE"))
                await conn.execute(text(
                    "UPDATE platform_saas_payment_catalog_methods "
                    "SET legacy_source='bank:RUB' WHERE id=:id"
                ), {"id":method})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _identity(conn,operator)
                await _call(conn,"platform_saas_payment_details_set",{
                    "text":"Replaced legacy bank reference 555888",
                },as_json=False)
                current=await _call(conn,"platform_saas_catalog_read",{})
                imported=next(x for x in current if x["id"]==str(method))
                assert imported["instructions"]=="Replaced legacy bank reference 555888"
                assert imported["name"]=="Bank transfer"
                await _call(conn,"platform_saas_payment_details_set",{
                    "text":None,
                },as_json=False)
                current=await _call(conn,"platform_saas_catalog_read",{})
                assert not next(x for x in current if x["id"]==str(method))["enabled"]
                await _call(conn,"platform_saas_payment_details_set",{
                    "text":"Stale keyboard cannot re-enable 555888",
                },as_json=False)
                current=await _call(conn,"platform_saas_catalog_read",{})
                assert not next(x for x in current if x["id"]==str(method))["enabled"]
                # Both reminder phases must consult the named catalog,
                # not the obsolete one-method-per-currency table.
                await conn.execute(text("RESET ROLE"))
                for fn in ("platform_saas_notice_schedule",
                           "platform_saas_notice_claim"):
                    body=(await conn.execute(text(
                        "SELECT pg_get_functiondef(("
                        ":signature)::regprocedure)"
                    ), {"signature":"public."+fn+"()"})).scalar_one()
                    assert "platform_saas_payment_catalog_currencies" in body
                    assert "platform_saas_payment_methods_v2" not in body
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _identity(conn,operator)
                await _call(conn,"platform_saas_catalog_remove",{
                    "id":method,
                },as_json=False)
                await conn.execute(text("RESET ROLE"))
                assert (await conn.execute(text(
                    "SELECT instructions_snapshot FROM "
                    "platform_saas_payment_attempts_v2 WHERE id=:id"
                ),{"id":UUID(str(attempt["id"]))})).scalar_one()==(
                    "Bank reference 123456789 for test"
                )
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()
