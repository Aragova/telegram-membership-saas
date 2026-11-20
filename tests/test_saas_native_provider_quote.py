"""RUB book ledger vs immutable native USD provider receipts.

Every test uses an isolated PostgreSQL transaction; no remote checkout,
customer bot, or actual payment is created.
"""
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import (
    ensure_telegram_actor,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and runtime RLS",
)


async def rpc(conn, name, *args):
    stmt = text("SELECT public." + name + "("
                + ",".join(":a" + str(i) for i in range(len(args)))
                + ") AS data").columns(data=JSONB)
    return (await conn.execute(stmt, {
        "a" + str(i): value for i, value in enumerate(args)
    })).scalar_one()


async def test_five_usd_invoice_stays_usd_in_both_gateway_quotes():
    settings = Settings()
    runtime = build_engine(settings)
    factory = build_session_factory(runtime)
    admin = create_async_engine(settings.migration_database_url)
    try:
        payer_tg = _tid()
        owner, merchant, project, bot, _ = await _project(factory, payer_tg)
        operator = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username=None, first_name=None,
        )
        async with admin.connect() as conn:
            outer = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO public.platform_saas_daily_fx_rates "
                    "(rate_date,currency_code,rub_per_unit,source_name,"
                    "source_date,published_by) VALUES "
                    "((clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date,"
                    "'USD',80,'CBR',"
                    "(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date,:op)"
                    " ON CONFLICT(rate_date,currency_code) DO NOTHING"
                ), {"op": operator})
                invoice = await _invoice(
                    conn, owner, merchant, bot, total="400.00",
                )
                await conn.execute(text(
                    "INSERT INTO public.platform_saas_invoice_lines "
                    "(id,invoice_id,project_id,source_kind,source_id,"
                    "bot_instance_id,original_amount,original_currency,"
                    "rub_per_unit,fee_rate,charge_rub) VALUES "
                    "(:id,:invoice,:project,'bot_fee',:bot,:bot,"
                    "5,'USD',80,1,400)"
                ), {
                    "id": uuid4(), "invoice": invoice,
                    "project": project, "bot": bot,
                })
                await conn.execute(text(
                    "UPDATE public.platform_saas_invoices "
                    "SET status='issued' WHERE id=:invoice"
                ), {"invoice": invoice})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(owner)})
                lava = await rpc(
                    conn, "platform_saas_gateway_reserve",
                    invoice, "lava", payer_tg, 998877,
                )
                crypto = await rpc(
                    conn, "platform_saas_gateway_reserve",
                    invoice, "crypto", payer_tg, 998877,
                )
                for quote in (lava, crypto):
                    assert quote["pay_currency"] == "USD"
                    assert Decimal(quote["pay_amount"]) == Decimal("5.00")
                    assert Decimal(quote["amount_rub"]) == Decimal("400")
                    assert quote["id"] != invoice
                # Operator binds and credits only the exact original
                # invoice, notwithstanding the provider's $5 amount.
                await conn.execute(text("RESET ROLE"))
                op_tg = (await conn.execute(text(
                    "SELECT telegram_user_id FROM telegram_users WHERE id=:id"
                ), {"id": operator})).scalar_one()
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": op_tg})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(operator)})
                ref = str(uuid4())
                bound = await rpc(
                    conn, "platform_saas_gateway_bind",
                    lava["id"], payer_tg, 998877, ref,
                    "https://app.lava.top/pay/verified-fixture",
                )
                assert bound is True
                credit = await rpc(
                    conn, "platform_saas_gateway_record_charge",
                    lava["id"], "lava", ref, Decimal("400"), datetime.now(UTC),
                )
                assert credit["applied"]
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(owner)})
                balance = await rpc(
                    conn, "platform_saas_v2_invoice_balance", invoice,
                )
                assert Decimal(balance["remaining_rub"]) == 0
                # A second provider can never allocate the same bot debt.
                await conn.execute(text("RESET ROLE"))
                count = (await conn.execute(text(
                    "SELECT count(*) FROM public.platform_saas_gateway_charges "
                    "WHERE invoice_id=:invoice"
                ), {"invoice": invoice})).scalar_one()
                assert count == 1
                # Native USD refund must resolve SQL aliases separately from
                # the PL/pgSQL row records and reopen the exact RUB book debt.
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(operator)})
                refund_id = "native-usd-refund-" + uuid4().hex
                refunded = await rpc(
                    conn, "platform_saas_gateway_operator_refund_native",
                    "lava", ref, refund_id, Decimal("2.00"), "USD",
                )
                assert refunded["recorded"]
                assert Decimal(refunded["amount_native"]) == Decimal("2.00")
                assert Decimal(refunded["amount_rub"]) == Decimal("160.00")
                replay = await rpc(
                    conn, "platform_saas_gateway_operator_refund_native",
                    "lava", ref, refund_id, Decimal("2.00"), "USD",
                )
                assert not replay["recorded"]
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(owner)})
                reopened = await rpc(
                    conn, "platform_saas_v2_invoice_balance", invoice,
                )
                assert Decimal(reopened["remaining_rub"]) == Decimal("160.00")
            finally:
                await outer.rollback()
    finally:
        await admin.dispose()
        await runtime.dispose()
