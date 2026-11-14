"""Regression: historic partial payments preserve native USD service fractions.

All banking, FX, invoice and operator rows live only in a rolled-back CI
PostgreSQL transaction. No real payment or Telegram action is performed.
"""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_platform_saas_invoice_rls import _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL, rollback-only",
)


async def _call(conn, rpc, *, as_json=True, **kwargs):
    statement = text(
        "SELECT public." + rpc + "(" +
        ",".join(":" + key for key in kwargs) + ") AS snapshot"
    )
    if as_json:
        statement = statement.columns(snapshot=JSONB)
    return (await conn.execute(statement, kwargs)).scalar_one()


async def _identity(conn, user):
    await conn.execute(text(
        "SELECT set_config('app.current_user_id',:actor,true)"
    ), {"actor": str(user)})


async def test_paid_half_of_three_dollars_stays_one_dollar_fifty():
    engine = create_async_engine(Settings().migration_database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                owner, merchant, project, bot, _ = await _project(
                    factory, _tid(),
                )
                operator_tg = _tid()
                operator = await ensure_telegram_actor(
                    factory, telegram_id=operator_tg,
                    username=None, first_name=None,
                )
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES (:tg,'owner')"
                ), {"tg": operator_tg})
                now = datetime.now(UTC)
                today = now.astimezone(ZoneInfo("Europe/Moscow")).date()
                issue_day = today - timedelta(days=1)
                original_issued = now - timedelta(days=1)
                for day, rate in (
                    (issue_day, Decimal("100")),
                    (today, Decimal("120")),
                ):
                    await conn.execute(text(
                        "INSERT INTO public.platform_saas_daily_fx_rates("
                        "rate_date,currency_code,rub_per_unit,source_name,"
                        "source_date,published_by) VALUES("
                        ":day,'USD',:rate,'CBR',:day,:operator)"
                    ), {
                        "day": day, "rate": rate, "operator": operator,
                    })
                invoice=uuid4()
                await conn.execute(text(
                    "INSERT INTO public.platform_saas_invoices("
                    "id,invoice_key,merchant_id,owner_user_id,kind,"
                    "bot_instance_id,period_start,period_end,due_at,"
                    "total_rub,created_at)"
                    " VALUES(:id,:key,:merchant,:owner,'prepayment',"
                    ":bot,:start,:end,:start,300,:issued)"
                ), {
                    "id": invoice, "key": "usd-fraction:" + str(uuid4()),
                    "merchant": merchant, "owner": owner, "bot": bot,
                    "start": now, "end": now + timedelta(days=30),
                    "issued": original_issued,
                })
                await _line(
                    conn, invoice, project, "bot_fee", bot,
                    amount="3", currency="USD", rate="100",
                    charge="300",
                )
                await conn.execute(text(
                    "UPDATE public.platform_saas_invoices "
                    "SET status='issued' WHERE id=:id"
                ), {"id": invoice})
                # A real operator is the only party authorized to confirm
                # historic payment evidence, including rollback-only fixtures.
                await _identity(conn, operator)
                # Legacy fractional bank payments use immutable v2
                # quote/proof/allocation records. Direct receipt insertion
                # is rightly forbidden for incomplete service settlement.
                old_attempt = uuid4()
                await conn.execute(text(
                    "INSERT INTO public.platform_saas_payment_attempts_v2("
                    "id,invoice_id,owner_user_id,amount_rub,currency_code,"
                    "rub_per_unit,amount_currency,quote_date,source_date,"
                    "instructions_snapshot,expires_at) VALUES("
                    ":id,:invoice,:owner,150,'RUB',1,150,:day,NULL,"
                    "'Historical verified bank transfer',:expiry)"
                ), {
                    "id": old_attempt, "invoice": invoice, "owner": owner,
                    "day": issue_day, "expiry": now + timedelta(days=1),
                })
                await conn.execute(text(
                    "INSERT INTO public.platform_saas_payment_proofs_v2("
                    "id,attempt_id,source_bot_id,source_chat_id,"
                    "source_message_id,source_file_id,file_type) VALUES("
                    ":id,:attempt,123987123,123987456,12345,"
                    "'historical-bank-proof-file','photo')"
                ), {"id": uuid4(), "attempt": old_attempt})
                await conn.execute(text(
                    "INSERT INTO public.platform_saas_payment_allocations_v2("
                    "id,attempt_id,invoice_id,received_currency,"
                    "received_amount,allocated_rub,bank_reference,confirmed_by)"
                    " VALUES(:id,:attempt,:invoice,'RUB',150,150,"
                    "'OLD-PAYMENT-CONFIRMED-001',:operator)"
                ), {
                    "id": uuid4(), "attempt": old_attempt,
                    "invoice": invoice, "operator": operator,
                })
                assert (await conn.execute(text(
                    "SELECT public.platform_saas_v2_remaining(:invoice)"
                ), {"invoice": invoice})).scalar_one() == Decimal("150")

                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _identity(conn, operator)
                await _call(
                    conn, "platform_saas_v2_bank_method",
                    currency="USD", instructions="Approved USD account for CI",
                    as_json=False,
                )
                methods = await _call(conn, "platform_saas_catalog_read")
                method = next(
                    x for x in methods if "USD" in x["currencies"]
                    and x["enabled"]
                    and x["instructions"] == "Approved USD account for CI"
                )
                await _identity(conn, owner)
                args = dict(invoice=invoice, amount_rub=Decimal("150"),
                            currency="USD")
                modern = await _call(
                    conn, "platform_saas_v3_create_attempt",
                    **args, method=UUID(str(method["id"])),
                )
                historical = await _call(
                    conn, "platform_saas_v2_create_attempt", **args,
                )
                for attempt in (modern, historical):
                    assert Decimal(str(attempt["amount"])) == Decimal("1.50")
                    assert Decimal(str(attempt["amount_rub"])) == Decimal("150")
                    assert attempt["currency"] == "USD"
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
