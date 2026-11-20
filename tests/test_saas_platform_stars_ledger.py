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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL with real membership_runtime role",
)


async def call(conn, name, *args, json=False):
    stmt = text(
        "SELECT public." + name + "("
        + ",".join(":a" + str(i) for i in range(len(args))) + ") AS result"
    )
    if json:
        stmt = stmt.columns(result=JSONB)
    return (await conn.execute(
        stmt, {"a" + str(i): arg for i, arg in enumerate(args)},
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
    return owner_tg, owner, operator, invoice, project


async def test_central_stars_settlement_replay_refund_and_bank_cap():
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
                owner_tg, owner, operator, invoice, project = await prepare(factory, conn)
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, operator)
                policy = await call(
                    conn, "platform_saas_stars_usd_policy_set",
                    Decimal("0.013"), Decimal("5"), True, json=True,
                )
                assert policy["enabled"] is True
                assert policy["example_stars"] == 405
                await set_actor(conn, owner)
                data = await call(
                    conn, "platform_saas_stars_create_order",
                    invoice, owner_tg, 987654321, json=True,
                )
                assert data["stars_amount"] == 405
                assert Decimal(data["usd_quote"]) == Decimal("5")
                assert Decimal(data["withdrawal_reserve_pct"]) == 5
                assert Decimal(data["rub_amount"]) == Decimal("150")
                payload = data["payload"]
                assert payload.startswith("vps:")
                assert not await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 9999999, 405,
                ), "Cannot pay a central invoice on the wrong Telegram bot"
                assert not await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 987654321, 59,
                )
                # A suspended USD payout policy must block authorization
                # even if the payer created an order while it was active.
                await set_actor(conn, operator)
                suspended = await call(
                    conn, "platform_saas_stars_usd_policy_set",
                    Decimal("0.013"), Decimal("5"), False, json=True,
                )
                assert suspended["enabled"] is False
                await set_actor(conn, owner)
                assert not await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 987654321, 405,
                )
                await set_actor(conn, operator)
                await call(
                    conn, "platform_saas_stars_usd_policy_set",
                    Decimal("0.013"), Decimal("5"), True, json=True,
                )
                await set_actor(conn, owner)
                assert await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 987654321, 405,
                )
                assert not await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 987654321, 405,
                ), "Same order cannot pass pre-checkout a second time"
                charge = "synthetic-star-charge-" + uuid4().hex
                paid_at = datetime.now(UTC)
                result = await call(
                    conn, "platform_saas_stars_record_charge",
                    payload, owner_tg, 987654321, 405, charge, paid_at,
                    json=True,
                )
                assert result["applied"] and not result["duplicate"]
                assert Decimal(result["allocated_rub"]) == Decimal("150")
                assert await call(
                    conn, "platform_saas_stars_charge_payload",
                    charge, owner_tg, 987654321,
                ) == payload
                assert await call(
                    conn, "platform_saas_stars_charge_payload",
                    charge, owner_tg, 111111111,
                ) is None, "Refund lookup must bind to the original central bot"
                # Even an administrative maintenance write cannot erase
                # a legitimate charged invoice without refund evidence.
                await conn.execute(text("RESET ROLE"))
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "UPDATE platform_saas_invoices "
                            "SET status='void' WHERE id=:invoice"
                        ), {"invoice": invoice})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, owner)
                replay = await call(
                    conn, "platform_saas_stars_record_charge",
                    payload, owner_tg, 987654321, 405, charge, paid_at,
                    json=True,
                )
                assert replay["applied"] and replay["duplicate"]
                # An unexpected SECOND real provider charge is not the
                # same as Telegram redelivering the first update.
                extra = "second-star-charge-" + uuid4().hex
                unapplied = await call(
                    conn, "platform_saas_stars_record_charge",
                    payload, owner_tg, 987654321, 405, extra,
                    datetime.now(UTC), json=True,
                )
                assert not unapplied["applied"] and unapplied["requires_review"]
                assert not unapplied["duplicate"]
                assert await call(
                    conn, "platform_saas_stars_record_refund",
                    payload, owner_tg, 987654321, 405,
                    extra, datetime.now(UTC),
                )
                report = await call(
                    conn, "platform_saas_merchant_bill_summary",
                    datetime.now(UTC).date().replace(day=1),
                    project, json=True,
                )
                assert Decimal(report["paid_rub"]) == Decimal("150")
                assert Decimal(report["credit_rub"]) == 0
                balance = await call(
                    conn, "platform_saas_v2_invoice_balance", invoice,
                    json=True,
                )
                assert balance["paid"] and Decimal(balance["stars_rub"]) == 150
                assert Decimal(balance["remaining_rub"]) == 0
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
                    payload, owner_tg, 987654321, 405,
                    charge, datetime.now(UTC),
                )
                assert not await call(
                    conn, "platform_saas_stars_record_refund",
                    payload, owner_tg, 987654321, 405,
                    charge, datetime.now(UTC),
                )
                balance = await call(
                    conn, "platform_saas_v2_invoice_balance",
                    invoice, json=True,
                )
                assert not balance["paid"] and Decimal(balance["stars_rub"]) == 0
                assert Decimal(balance["remaining_rub"]) == 150
                assert not await call(
                    conn, "platform_saas_stars_precheckout",
                    payload, owner_tg, 987654321, 405,
                ), "Refunded order must never be payable again"
            finally:
                await outer.rollback()
    finally:
        await admin.dispose()


async def test_stars_price_is_disabled_until_operator_sets_it_and_owned_bills_only():
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
                owner_tg, owner, operator, invoice, project = await prepare(factory, conn)
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
                            conn, "platform_saas_stars_usd_policy_set",
                            Decimal("0.013"), Decimal("5"), True, json=True,
                        )
                await set_actor(conn, operator)
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await call(
                            conn, "platform_saas_stars_usd_policy_set",
                            Decimal("0.001"), Decimal("5"), True, json=True,
                        )
                policy = await call(
                    conn, "platform_saas_stars_usd_policy_set",
                    Decimal("0.013"), Decimal("5"), True, json=True,
                )
                assert policy["enabled"] is True
                assert policy["example_stars"] == 405
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
        await admin.dispose()


async def test_bank_clears_invoice_after_precheckout_stars_charge_is_not_credited():
    """A real Stars charge is still journaled after a parallel bank settlement."""
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
                owner_tg, owner, operator, invoice, project = await prepare(factory, conn)
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await set_actor(conn, operator)
                await call(
                    conn, "platform_saas_stars_usd_policy_set",
                    Decimal("0.013"), Decimal("5"), True, json=True,
                )
                await set_actor(conn, owner)
                quote = await call(
                    conn, "platform_saas_stars_create_order",
                    invoice, owner_tg, 987654321, json=True,
                )
                assert await call(
                    conn, "platform_saas_stars_precheckout",
                    quote["payload"], owner_tg, 987654321, 405,
                )
                # A verified bank payment wins the race during Telegram
                # authorization/settlement. Do not allocate another 150 RUB.
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
                await set_actor(conn, owner)
                result = await call(
                    conn, "platform_saas_stars_record_charge",
                    quote["payload"], owner_tg, 987654321, 405,
                    "tg-race-" + uuid4().hex, datetime.now(UTC), json=True,
                )
                assert not result["applied"]
                assert result["requires_review"]
                balance = await call(
                    conn, "platform_saas_v2_invoice_balance", invoice,
                    json=True,
                )
                assert balance["paid"]
                assert Decimal(balance["stars_rub"]) == 0
                assert Decimal(balance["remaining_rub"]) == 0
            finally:
                await outer.rollback()
    finally:
        await admin.dispose()


async def test_precheckout_policy_row_share_lock_blocks_concurrent_disable():
    """PostgreSQL guarantee behind the USD precheckout/suspend serialization.

    The real precheckout function holds FOR SHARE on this singleton row,
    so /saasstarsrate off cannot complete before that transaction finishes.
    Both synthetic transactions roll back without changing the singleton.
    """
    cfg = Settings()
    engine = create_async_engine(cfg.migration_database_url)
    try:
        async with engine.connect() as checking, engine.connect() as disabling:
            first = await checking.begin()
            second = await disabling.begin()
            try:
                row = (await checking.execute(text(
                    "SELECT id FROM public.platform_saas_stars_usd_policy "
                    "WHERE id=1 FOR SHARE"
                ))).scalar_one()
                assert row == 1
                await disabling.execute(text(
                    "SET LOCAL lock_timeout = '150ms'"
                ))
                with pytest.raises(DBAPIError) as blocked:
                    await disabling.execute(text(
                        "UPDATE public.platform_saas_stars_usd_policy "
                        "SET enabled=false WHERE id=1"
                    ))
                assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
            finally:
                await second.rollback()
                await first.rollback()
    finally:
        await engine.dispose()
