"""Payment of a historical SaaS invoice must survive bot retirement.

Every synthetic finance event is inside a rolled-back PostgreSQL transaction.
No live Telegram, real payment or actual service activation occurs.
"""
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
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
    return (await conn.execute(
        sql, {"p" + str(i): arg for i, arg in enumerate(args)},
    )).scalar_one()


async def retired_invoice(conn, invoice):
    """The historical invoice keeps its immutable old bot UUID."""
    original_bot = (await conn.execute(text(
        "SELECT bot_instance_id FROM public.platform_saas_invoices "
        "WHERE id=:invoice",
    ), {"invoice": invoice})).scalar_one()
    assert original_bot is not None
    await conn.execute(text(
        "DELETE FROM public.bot_instances WHERE id=:bot"
    ), {"bot": original_bot})
    assert (await conn.execute(text(
        "SELECT count(*) FROM public.bot_instances WHERE id=:bot"
    ), {"bot": original_bot})).scalar_one() == 0
    assert (await conn.execute(text(
        "SELECT bot_instance_id FROM public.platform_saas_invoices "
        "WHERE id=:invoice"
    ), {"invoice": invoice})).scalar_one() == original_bot
    return original_bot


async def assert_no_ghost_activation(conn, invoice):
    # The activation ledger is RLS-protected; inspect only as DB owner.
    await conn.execute(text("RESET ROLE"))
    assert (await conn.execute(text(
        "SELECT count(*) FROM public.platform_saas_bot_service_activations "
        "WHERE invoice_id=:invoice"
    ), {"invoice": invoice})).scalar_one() == 0


@pytest.mark.parametrize("provider", ("lava", "crypto"))
async def test_historical_invoice_gateway_still_settles_without_ghost_service(
    provider,
):
    config = Settings()
    engine = create_async_engine(config.migration_database_url)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                payer_tg, payer, operator, invoice, _ = await prepare(
                    factory, conn,
                )
                await retired_invoice(conn, invoice)

                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, payer)
                historical = await rpc(
                    conn, "platform_saas_owner_invoice_report", 0, 6, json=True,
                )
                assert historical["unpaid"] >= 1
                assert any(str(item["id"]) == str(invoice)
                           for item in historical["invoices"])

                order = await rpc(
                    conn, "platform_saas_gateway_reserve",
                    invoice, provider, payer_tg, 987654321, json=True,
                )
                assert order["status"] == "reserved"
                provider_id = str(uuid4()) if provider == "lava" else "847162930"
                await set_actor(conn, operator)
                assert await rpc(
                    conn, "platform_saas_gateway_bind",
                    order["id"], payer_tg, 987654321, provider_id,
                    "https://t.me/CryptoBot?start=IV847162930",
                )
                receipt = await rpc(
                    conn, "platform_saas_gateway_record_charge",
                    order["id"], provider, provider_id,
                    Decimal("150"), datetime.now(UTC), json=True,
                )
                assert receipt["applied"] is True
                assert receipt["requires_review"] is False
                assert await rpc(
                    conn, "platform_saas_gateway_record_charge",
                    order["id"], provider, provider_id,
                    Decimal("150"), datetime.now(UTC), json=True,
                ) == {
                    "duplicate": True,
                    "applied": True,
                    "applied_rub": "150.00",
                }
                await set_actor(conn, payer)
                balance = await rpc(
                    conn, "platform_saas_v2_invoice_balance", invoice, json=True,
                )
                assert balance["paid"]
                assert Decimal(balance["remaining_rub"]) == 0
                await assert_no_ghost_activation(conn, invoice)
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()


async def test_retired_service_invoice_accepts_native_stars_without_activation():
    config = Settings()
    engine = create_async_engine(config.migration_database_url)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                payer_tg, payer, operator, invoice, _ = await prepare(
                    factory, conn,
                )
                await retired_invoice(conn, invoice)
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, operator)
                await rpc(conn, "platform_saas_stars_set_rate", Decimal("2.50"))
                await set_actor(conn, payer)
                order = await rpc(
                    conn, "platform_saas_stars_create_order",
                    invoice, payer_tg, 987654321, json=True,
                )
                assert order["stars_amount"] == 60
                assert await rpc(
                    conn, "platform_saas_stars_precheckout",
                    order["payload"], payer_tg, 987654321, 60,
                )
                charge = await rpc(
                    conn, "platform_saas_stars_record_charge",
                    order["payload"], payer_tg, 987654321, 60,
                    "retired-historical-charge-" + uuid4().hex,
                    datetime.now(UTC), json=True,
                )
                assert charge["applied"]
                balance = await rpc(
                    conn, "platform_saas_v2_invoice_balance", invoice,
                    json=True,
                )
                assert balance["paid"]
                assert Decimal(balance["remaining_rub"]) == 0
                await assert_no_ghost_activation(conn, invoice)
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()


async def test_operator_verified_bank_proof_pays_retired_invoice():
    """Use the CURRENT owner proof -> operator approval, never the retired
    privileged platform_saas_confirm_receipt() legacy endpoint.
    """
    config = Settings()
    engine = create_async_engine(config.migration_database_url)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                payer_tg, payer, operator, invoice, _ = await prepare(
                    factory, conn,
                )
                await retired_invoice(conn, invoice)
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, operator)
                method_id = await rpc(
                    conn, "platform_saas_catalog_create",
                    "Test bank", "Test bank payment instructions",
                    "RUB",
                )
                await set_actor(conn, payer)
                attempt = await rpc(
                    conn, "platform_saas_v3_create_attempt",
                    invoice, Decimal("150.00"), "RUB", method_id,
                    json=True,
                )
                await rpc(
                    conn, "platform_saas_v2_submit_proof",
                    attempt["id"], 987654321, payer_tg, 12345,
                    "test-photo-file-id-123", "photo", "unique-file-123",
                )
                await set_actor(conn, operator)
                allocation = await rpc(
                    conn, "platform_saas_v2_approve_checked_attempt",
                    attempt["id"],
                )
                assert allocation is not None
                await set_actor(conn, payer)
                balance = await rpc(
                    conn, "platform_saas_v2_invoice_balance", invoice,
                    json=True,
                )
                assert Decimal(balance["remaining_rub"]) == 0
                await assert_no_ghost_activation(conn, invoice)
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
