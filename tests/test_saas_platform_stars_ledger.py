"""Rollback-only PostgreSQL tests for central platform Stars invoice payments.

All bot IDs and charges are synthetic CI fixtures. No real Telegram Stars are
charged, no production bots touched, and the outer transaction is rolled back.
"""
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid
from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL with real membership_runtime role",
)


async def call(conn, name, *args, json=False):
    stmt = text(
        "SELECT public." + name + "("
        + ",".join(":" + str(i) for i in range(len(args))) + ") AS result"
    )
    if json:
        stmt = stmt.columns(result=JSONB)
    return (await conn.execute(
        stmt, {str(i): arg for i, arg in enumerate(args)},
    )).scalar_one()


async def set_actor(conn, actor):
    await conn.execute(
        text("SELECT set_config('app.current_user_id',:actor,true)"),
        {"actor": str(actor)},
    )


async def prepare(factory, conn):
    owner_tg = _tid()
    owner, merchant, project, bot, _ = await _project(factory, owner_tg)
    operator_tg = _tid()
    operator = await ensure_telegram_actor(
        factory, telegram_id=operator_tg, username=None, first_name=None,
    )
    await conn.execute(text(
        "INSERT INTO platform_operators (telegram_user_id,role) "
        "VALUES (:tg,'owner')"
    ), {"tg": operator_tg})
    invoice = await _invoice(conn, owner, merchant, bot, total="150.00")
    await _line(conn, invoice, project, "bot_fee", bot)
    await conn.execute(text(
        "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
    ), {"id": invoice})
    return owner_tg, owner, operator, invoice


async def test_central_stars_settlement_replay_refund_and_bank_cap():
    cfg = Settings()
    runtime = build_engine(cfg)
    factory = build_session_factory(runtime)
    admin = create_async_engine(cfg.migration_database_url)
    try:
        async with admin.connect() as conn:
            outer = await conn.begin()
            try:
                owner_tg, owner, operator, invoice = await prepare(factory, conn)
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, operator)
                assert await call(
                    conn, "platform_saas_stars_set_rate",
                    Decimal("2.50"),
                ) == Decimal("2.50")
                await set_actor(conn, owner)
                data = await call(
                    conn, "platform_saas_stars_create_order",
                    invoice, owner_tg, 987654321, json=True,
                )
                assert data["stars_amount"] == 60
                assert Decimal(data["rub_amount"]) == Decimal("150")
                payload = data["payload"]
                assert payload.startswith("vps:")
                assert not await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 9999999, 60,
                ), "Cannot pay a central invoice on the wrong Telegram bot"
                assert not await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 987654321, 59,
                )
                assert await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 987654321, 60,
                )
                charge = "synthetic-star-charge-" + uuid4().hex
                paid_at = datetime.now(UTC)
                result = await call(
                    conn, "platform_saas_stars_record_charge",
                    payload, owner_tg, 987654321, 60, charge, paid_at,
                    json=True,
                )
                assert result["applied"] and not result["duplicate"]
                assert Decimal(result["allocated_rub"]) == Decimal("150")
                replay = await call(
                    conn, "platform_saas_stars_record_charge",
                    payload, owner_tg, 987654321, 60, charge, paid_at,
                    json=True,
                )
                assert replay["applied"] and replay["duplicate"]
                assert await call(
                    conn, "platform_saas_v2_remaining", invoice,
                ) == Decimal("0.00")
                balance = await call(
                    conn, "platform_saas_v2_invoice_balance", invoice,
                    json=True,
                )
                assert balance["paid"] and Decimal(balance["stars_rub"]) == 150
                # No duplicated payment can be inserted as a legacy receipt.
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "INSERT INTO platform_saas_receipts "
                            "(id,invoice_id,confirmed_by,amount_rub) "
                            "VALUES(:id,:invoice,:actor,150)"
                        ), {
                            "id": uuid4(), "invoice": invoice, "actor": operator,
                        })
                assert await call(
                    conn, "platform_saas_stars_record_refund",
                    payload, owner_tg, 987654321, 60,
                    charge, datetime.now(UTC),
                )
                assert not await call(
                    conn, "platform_saas_stars_record_refund",
                    payload, owner_tg, 987654321, 60,
                    charge, datetime.now(UTC),
                )
                assert await call(
                    conn, "platform_saas_v2_remaining", invoice,
                ) == Decimal("150.00")
                balance = await call(
                    conn, "platform_saas_v2_invoice_balance",
                    invoice, json=True,
                )
                assert not balance["paid"] and Decimal(balance["stars_rub"]) == 0
                assert not await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 987654321, 60,
                ), "Refunded order must never be payable again"
            finally:
                await outer.rollback()
    finally:
        await runtime.dispose()
        await admin.dispose()


async def test_stars_price_is_disabled_until_operator_sets_it_and_owned_bills_only():
    cfg = Settings()
    runtime = build_engine(cfg)
    factory = build_session_factory(runtime)
    admin = create_async_engine(cfg.migration_database_url)
    try:
        async with admin.connect() as conn:
            outer = await conn.begin()
            try:
                owner_tg, owner, operator, invoice = await prepare(factory, conn)
                stranger_tg = _tid()
                stranger = await ensure_telegram_actor(
                    factory, telegram_id=stranger_tg,
                    username=None, first_name=None,
                )
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, owner)
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await call(
                            conn, "platform_saas_stars_create_order",
                            invoice, owner_tg, 987654321, json=True,
                        )
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await call(
                            conn, "platform_saas_stars_set_rate",
                            Decimal("0.01"),
                        )
                await set_actor(conn, operator)
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await call(
                            conn, "platform_saas_stars_set_rate",
                            Decimal("0.001"),
                        )
                assert await call(
                    conn, "platform_saas_stars_set_rate",
                    Decimal("2.50"),
                ) == Decimal("2.50")
                await set_actor(conn, stranger)
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await call(
                            conn, "platform_saas_stars_create_order",
                            invoice, stranger_tg, 987654321, json=True,
                        )
            finally:
                await outer.rollback()
    finally:
        await runtime.dispose()
        await admin.dispose()
