"""Append-only ViMi Pay partial payments against an actual issued invoice.

No real money moves. Operator rows, payment methods and invoice records are
seeded only in a privileged PostgreSQL transaction that is ALWAYS rolled back.
"""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import (
    _invoice,
    _line,
    _project,
    _tid,
)

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_notices import _notice_text

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated PostgreSQL, membership_runtime and owner-only RPC required",
)


async def _call(conn, name, args):
    params = ",".join(":" + k for k in args)
    return (await conn.execute(
        text(f"SELECT public.{name}({params}) AS result").columns(result=JSONB),
        args,
    )).scalar_one()


async def _actor(conn, user):
    await conn.execute(
        text("SELECT set_config('app.current_user_id',:actor,true)"),
        {"actor": str(user)},
    )


async def test_partial_fiat_bank_proofs_cannot_overpay_or_double_credit():
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    owner_tg, operator_tg = _tid(), _tid()
    try:
        owner, merchant, project, bot, _ = await _project(factory, owner_tg)
        outsider, _, _, _, _ = await _project(factory, _tid())
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg,
            username=None, first_name=None,
        )
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO platform_operators (telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": operator_tg})
                when = datetime.now(UTC) + timedelta(minutes=1)
                invoice = await _invoice(
                    conn, owner, merchant, bot, period_start=when,
                )
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued'"
                    " WHERE id=:id"
                ), {"id": invoice})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _actor(conn, operator)
                assert await _call(conn, "platform_saas_v2_bank_method", {
                    "currency": "RUB", "instructions": "ViMi bank RUB test details",
                })
                assert await _call(conn, "platform_saas_v2_bank_method", {
                    "currency": "USD", "instructions": "ViMi USD bank test details",
                })
                await conn.execute(text("RESET ROLE"))
                await conn.execute(text(
                    "INSERT INTO public.platform_saas_daily_fx_rates("
                    "rate_date,currency_code,rub_per_unit,source_name,"
                    "source_date,published_by) VALUES("
                    "(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date,"
                    "'USD',95,'CBR',"
                    "(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date,"
                    ":operator) ON CONFLICT DO NOTHING"
                ), {"operator": operator})
                # Legacy RUB details must never override an explicit v2
                # disable, even when a buyer has an issued account invoice.
                await conn.execute(text(
                    "UPDATE platform_saas_payment_details SET "
                    "instructions='Legacy RUB account (retired)' WHERE id=1"
                ))
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _actor(conn, operator)
                assert await _call(conn, "platform_saas_v2_bank_method", {
                    "currency": "RUB", "instructions": None,
                })
                await _actor(conn, owner)
                methods = await _call(
                    conn, "platform_saas_v2_available_methods",
                    {"invoice": invoice},
                )
                assert "RUB" not in methods
                assert "USD" in methods
                report = await _call(
                    conn, "platform_saas_owner_invoice_report",
                    {"page": 0, "size": 6},
                )
                assert report["details_configured"] is False
                assert report["instructions"] is None
                with pytest.raises(DBAPIError) as retired:
                    async with conn.begin_nested():
                        await _call(
                            conn, "platform_saas_v2_create_attempt",
                            {"invoice": invoice,
                             "amount_rub": Decimal("100"),
                             "currency": "RUB"},
                        )
                assert getattr(retired.value.orig, "sqlstate", None) == "42501"
                await _actor(conn, operator)
                assert await _call(conn, "platform_saas_v2_bank_method", {
                    "currency": "RUB", "instructions": "ViMi bank RUB test details",
                })
                await _actor(conn, owner)
                usd_quote = await _call(
                    conn, "platform_saas_v2_create_attempt",
                    {"invoice": invoice, "amount_rub": Decimal("150"),
                     "currency": "USD"},
                )
                assert Decimal(usd_quote["amount_rub"]) == Decimal("150")
                assert Decimal(usd_quote["amount"]) > 0
                # FX cents are quoted, the exact RUB debt is not rewritten.
                assert usd_quote["currency"] == "USD"
                quote = await _call(conn, "platform_saas_v2_create_attempt", {
                    "invoice": invoice, "amount_rub": Decimal("100"),
                    "currency": "RUB",
                })
                assert quote["amount"] == "100.00"
                assert Decimal(quote["amount_rub"]) == Decimal("100")
                assert "bank RUB" in quote["instructions"]
                attempt_id = quote["id"]
                await _actor(conn, outsider)
                with pytest.raises(DBAPIError) as forbidden:
                    async with conn.begin_nested():
                        await _call(conn, "platform_saas_v2_create_attempt", {
                            "invoice": invoice, "amount_rub": Decimal("1"),
                            "currency": "RUB",
                        })
                assert getattr(forbidden.value.orig, "sqlstate", None) == "42501"
                await _actor(conn, owner)
                proof = await _call(conn, "platform_saas_v2_submit_proof", {
                    "attempt": attempt_id, "bot": 123987123,
                    "chat": owner_tg, "message": 546, "file_id": "private-file-id-123",
                    "kind": "photo",
                })
                assert proof
                assert await _call(conn, "platform_saas_v2_submit_proof", {
                    "attempt": attempt_id, "bot": 123987123,
                    "chat": owner_tg, "message": 546, "file_id": "private-file-id-123",
                    "kind": "photo",
                }) == proof
                await _actor(conn, operator)
                first = await _call(conn, "platform_saas_v2_approve_attempt", {
                    "attempt": attempt_id, "reference": "BANK-VIMI-00001",
                })
                assert first
                assert await _call(conn, "platform_saas_v2_approve_attempt", {
                    "attempt": attempt_id, "reference": "BANK-VIMI-00001",
                }) == first
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await _call(conn, "platform_saas_v2_approve_attempt", {
                            "attempt": attempt_id,
                            "reference": "DIFFERENT-REPLAY-REF",
                        })
                await _actor(conn, owner)
                balance = await _call(conn, "platform_saas_v2_invoice_balance", {
                    "invoice": invoice,
                })
                assert balance["remaining_rub"] == "50.00"
                assert balance["partial_rub"] == "100.00"
                assert balance["paid"] is False
                merchant_view = await _call(
                    conn, "platform_saas_owner_invoice_report",
                    {"page": 0, "size": 6},
                )
                assert merchant_view["unpaid"] == 1
                assert Decimal(merchant_view["outstanding_rub"]) == Decimal("50")
                assert merchant_view["invoices"][0]["status"] == "unpaid"
                assert Decimal(
                    merchant_view["invoices"][0]["remaining_rub"]
                ) == Decimal("50")
                await _actor(conn, operator)
                central_view = await _call(
                    conn, "platform_saas_hold_invoice_report",
                    {"page": 0, "size": 10},
                )
                assert central_view["unpaid"] >= 1
                assert Decimal(central_view["unpaid_rub"]) >= Decimal("50")
                assert Decimal(central_view["received_rub"]) >= Decimal("100")
                own_invoice = next(
                    item for item in central_view["invoices"]
                    if item["id"] == str(invoice)
                )
                assert Decimal(own_invoice["remaining_rub"]) == Decimal("50")
                await _actor(conn, owner)
                with pytest.raises(DBAPIError) as too_big:
                    async with conn.begin_nested():
                        await _call(conn, "platform_saas_v2_create_attempt", {
                            "invoice": invoice, "amount_rub": Decimal("51"),
                            "currency": "RUB",
                        })
                assert getattr(too_big.value.orig, "sqlstate", None) == "23514"
                last = await _call(conn, "platform_saas_v2_create_attempt", {
                    "invoice": invoice, "amount_rub": Decimal("50"),
                    "currency": "RUB",
                })
                last_id = last["id"]
                await _call(conn, "platform_saas_v2_submit_proof", {
                    "attempt": last_id, "bot": 123987123,
                    "chat": owner_tg, "message": 547, "file_id": "private-file-id-456",
                    "kind": "document",
                })
                await _actor(conn, operator)
                assert await _call(conn, "platform_saas_v2_approve_attempt", {
                    "attempt": last_id, "reference": "BANK-VIMI-00002",
                })
                balance = await _call(conn, "platform_saas_v2_invoice_balance", {
                    "invoice": invoice,
                })
                assert balance["remaining_rub"] == "0.00"
                assert balance["paid"] is True
                await _actor(conn, owner)
                owner_view = await _call(
                    conn, "platform_saas_owner_invoice_report",
                    {"page": 0, "size": 6},
                )
                assert owner_view["unpaid"] == 0
                assert Decimal(owner_view["outstanding_rub"]) == 0
                assert owner_view["invoices"][0]["status"] == "paid"
                await _actor(conn, operator)
                central_view = await _call(
                    conn, "platform_saas_hold_invoice_report",
                    {"page": 0, "size": 10},
                )
                assert Decimal(central_view["received_rub"]) >= Decimal("150")
                own_invoice = next(
                    item for item in central_view["invoices"]
                    if item["id"] == str(invoice)
                )
                assert own_invoice["status"] == "paid"
                assert Decimal(own_invoice["remaining_rub"]) == 0
                await conn.execute(text("RESET ROLE"))
                with pytest.raises(DBAPIError) as voided:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "UPDATE public.platform_saas_invoices SET "
                            "status='void' WHERE id=:invoice"
                        ), {"invoice": invoice})
                assert getattr(voided.value.orig, "sqlstate", None) == "42501"
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await _actor(conn, operator)
                with pytest.raises(DBAPIError) as double:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "INSERT INTO platform_saas_receipts("
                            "id,invoice_id,confirmed_by,amount_rub) "
                            "VALUES(gen_random_uuid(),:invoice,:operator,150)"
                        ), {"invoice": invoice, "operator": operator})
                assert getattr(double.value.orig, "sqlstate", None) == "23514"
                await conn.execute(text("RESET ROLE"))
                assert (await conn.execute(text(
                    "SELECT enabled FROM platform_saas_billing_control WHERE id=1"
                ))).scalar_one() is False
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()



def test_billing_reminder_uses_live_invoice_navigation_not_stale_bank_details():
    item = {
        "kind": "overdue",
        "invoice_id": "11111111-1111-4111-8111-111111111111",
        "due_at": "2026-11-12T00:00:00+00:00",
        "amount_rub": "50.00",
        "instructions": "RETIRED BANK ACCOUNT 0000000000",
    }
    message = _notice_text(item)
    assert "RETIRED BANK ACCOUNT" not in message
    assert "50.00" in message
    assert "ViMi Pay" in message
