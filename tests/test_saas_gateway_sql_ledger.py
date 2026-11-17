"""Transactional PostgreSQL tests of platform external-provider cash accounting.

Every test, including synthetic users and bots, uses a single rolled-back
outer PostgreSQL transaction. No real provider calls or production money.
"""
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_saas_platform_stars_ledger import prepare, set_actor

from telegram_membership_saas.config import Settings

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL with runtime RLS",
)


async def rpc(conn, name, *args, json=False):
    sql = text(
        "SELECT public." + name + "("
        + ",".join(":p" + str(i) for i in range(len(args))) + ") AS result"
    )
    if json:
        sql = sql.columns(result=JSONB)
    return (await conn.execute(sql, {
        "p" + str(i): arg for i, arg in enumerate(args)
    })).scalar_one()


async def test_gateway_receipt_replay_refund_and_bank_race():
    cfg = Settings()
    admin = create_async_engine(cfg.migration_database_url)
    try:
        async with admin.connect() as conn:
            outer = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                payer_tg, payer, operator, invoice, project = await prepare(
                    factory, conn,
                )
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, payer)
                order = await rpc(
                    conn, "platform_saas_gateway_reserve",
                    invoice, "crypto", payer_tg, 99887766, json=True,
                )
                assert order["status"] == "reserved"
                assert order["payload"] == "vpc:" + str(order["id"])
                assert Decimal(order["amount_rub"]) == 150
                await set_actor(conn, operator)
                assert await rpc(
                    conn, "platform_saas_gateway_bind",
                    order["id"], payer_tg, 99887766, "4815162342",
                    "https://t.me/CryptoBot?start=IV4815162342",
                )
                await set_actor(conn, payer)
                # Only the platform operator can acknowledge a paid
                # *provider-authenticated* invoice; the merchant cannot.
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await rpc(
                            conn, "platform_saas_gateway_record_charge",
                            order["id"], "crypto", "4815162342",
                            Decimal("150"), datetime.now(UTC), json=True,
                        )
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT * FROM public.platform_saas_gateway_orders"
                        ))
                await set_actor(conn, operator)
                available = await rpc(
                    conn, "platform_saas_gateway_pending", 100, json=True,
                )
                assert any(str(x["id"]) == str(order["id"]) for x in available)
                paid_at = datetime.now(UTC)
                charge = await rpc(
                    conn, "platform_saas_gateway_record_charge",
                    order["id"], "crypto", "4815162342",
                    Decimal("150"), paid_at, json=True,
                )
                assert charge["applied"] and not charge["duplicate"]
                duplicate = await rpc(
                    conn, "platform_saas_gateway_record_charge",
                    order["id"], "crypto", "4815162342",
                    Decimal("150"), paid_at, json=True,
                )
                assert duplicate["duplicate"] and duplicate["applied"]
                await set_actor(conn, payer)
                balance = await rpc(
                    conn, "platform_saas_v2_invoice_balance",
                    invoice, json=True,
                )
                assert balance["paid"]
                assert Decimal(balance["gateway_rub"]) == 150
                assert Decimal(balance["remaining_rub"]) == 0
                report = await rpc(
                    conn, "platform_saas_merchant_bill_summary",
                    datetime.now(UTC).date().replace(day=1),
                    project, json=True,
                )
                assert Decimal(report["paid_rub"]) == 150
                assert Decimal(report["credit_rub"]) == 0
                await conn.execute(text("RESET ROLE"))
                await set_actor(conn, operator)
                op = await rpc(
                    conn, "platform_saas_invoice_report", 0, 10,
                    json=True,
                )
                assert Decimal(op["received_rub"]) >= 150
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text("RESET ROLE"))
                        await conn.execute(text(
                            "UPDATE platform_saas_invoices "
                            "SET status='void' WHERE id=:bill"
                        ), {"bill": invoice})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, operator)
                assert await rpc(
                    conn, "platform_saas_gateway_record_refund",
                    order["id"], "crypto", "4815162342",
                    "provider-refund-4815162342", Decimal("150"),
                    datetime.now(UTC),
                )
                assert not await rpc(
                    conn, "platform_saas_gateway_record_refund",
                    order["id"], "crypto", "4815162342",
                    "provider-refund-4815162342", Decimal("150"),
                    datetime.now(UTC),
                )
                await set_actor(conn, payer)
                new_balance = await rpc(
                    conn, "platform_saas_v2_invoice_balance",
                    invoice, json=True,
                )
                assert not new_balance["paid"]
                assert Decimal(new_balance["gateway_rub"]) == 0
                assert Decimal(new_balance["remaining_rub"]) == 150
            finally:
                await outer.rollback()
    finally:
        await admin.dispose()


async def test_gateway_credited_exactly_once_after_parallel_bank_payment():
    cfg = Settings()
    admin = create_async_engine(cfg.migration_database_url)
    try:
        async with admin.connect() as conn:
            outer = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                payer_tg, payer, operator, invoice, _ = await prepare(
                    factory, conn,
                )
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, payer)
                order = await rpc(
                    conn, "platform_saas_gateway_reserve",
                    invoice, "crypto", payer_tg, 99887766, json=True,
                )
                await set_actor(conn, operator)
                assert await rpc(
                    conn, "platform_saas_gateway_bind",
                    order["id"], payer_tg, 99887766, "4815162333",
                    "https://t.me/CryptoBot?start=IV4815162333",
                )
                await conn.execute(text("RESET ROLE"))
                await set_actor(conn, operator)
                await conn.execute(text(
                    "INSERT INTO platform_saas_receipts "
                    "(id,invoice_id,confirmed_by,amount_rub) "
                    "VALUES(:id,:invoice,:operator,150)"
                ), {
                    "id": uuid4(), "invoice": invoice, "operator": operator,
                })
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, operator)
                outcome = await rpc(
                    conn, "platform_saas_gateway_record_charge",
                    order["id"], "crypto", "4815162333",
                    Decimal("150"), datetime.now(UTC), json=True,
                )
                assert not outcome["applied"]
                assert outcome["requires_review"]
                await set_actor(conn, payer)
                balance = await rpc(
                    conn, "platform_saas_v2_invoice_balance",
                    invoice, json=True,
                )
                assert balance["paid"] and Decimal(balance["gateway_rub"]) == 0
                assert Decimal(balance["remaining_rub"]) == 0
            finally:
                await outer.rollback()
    finally:
        await admin.dispose()



async def test_gateway_pending_rotates_beyond_one_hundred_unpaid_invoices():
    """100 stuck payers cannot permanently hide the 101st paid provider."""
    cfg = Settings()
    admin = create_async_engine(cfg.migration_database_url)
    try:
        async with admin.connect() as conn:
            outer = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                payer_tg, payer, operator, invoice, _ = await prepare(
                    factory, conn,
                )
                orders = [uuid4() for _ in range(103)]
                # Admin-only test fixture inserts are transactional;
                # the runtime role is never granted direct table writes.
                await conn.execute(text("""
                    INSERT INTO public.platform_saas_gateway_orders(
                      id,invoice_id,owner_user_id,payer_telegram_id,
                      central_bot_id,provider,invoice_payload,amount_rub,
                      status,provider_invoice_id,provider_invoice_url,
                      expires_at
                    ) VALUES (
                      :id,:invoice,:owner,:payer,99887766,'crypto',
                      :payload,150,'issued',:provider_id,
                      'https://t.me/CryptoBot?start=test',
                      clock_timestamp()+interval '30 minutes'
                    )
                """), [
                    {
                        "id": oid, "invoice": invoice, "owner": payer,
                        "payer": payer_tg, "payload": "vpc:" + str(oid),
                        "provider_id": str(i + 100_000),
                    }
                    for i, oid in enumerate(orders)
                ])
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, operator)
                first = await rpc(
                    conn, "platform_saas_gateway_pending", 100, json=True,
                )
                assert len(first) == 100
                later = await rpc(
                    conn, "platform_saas_gateway_pending", 100, json=True,
                )
                assert len(later) == 100
                first_ids = {row["id"] for row in first}
                later_ids = {row["id"] for row in later}
                assert len(later_ids-first_ids) == 3
                assert len(first_ids | later_ids) == 103
            finally:
                await outer.rollback()
    finally:
        await admin.dispose()
