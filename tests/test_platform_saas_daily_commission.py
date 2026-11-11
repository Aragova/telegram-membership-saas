"""Rollback-only PostgreSQL E2E of monthly FX in the real commission issuer.

This closes the exact-HEAD reviewer concern: not merely inspecting SQL text.
All commercial switch changes, FX and invoices exist only in one rolled-back
test transaction. No user balances or staging data are touched.
"""
import os
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL with privileged rollback-only tests",
)


async def _commission(conn, merchant, owner, month, fx="{}"):
    return (await conn.execute(text(
        "SELECT public.platform_saas_issue_commission_invoice("
        ":m,:o,:month,CAST(:fx AS jsonb))"
    ), {"m": merchant, "o": owner, "month": month, "fx": fx})).scalar_one()


async def test_monthly_fx_used_for_real_approved_sales_and_idempotent_invoice():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    operator_tg = _tid()
    moscow = ZoneInfo("Europe/Moscow")
    now = datetime.now(UTC)
    # Previous Moscow month has ended: the real issuer refuses current month.
    current_month = now.astimezone(moscow).date().replace(day=1)
    last_day_previous = current_month - timedelta(days=1)
    month = last_day_previous.replace(day=1)
    paid_day = month + timedelta(days=5)
    second_day = paid_day + timedelta(days=1)
    paid_at = datetime.combine(paid_day, time(12), moscow).astimezone(UTC)
    paid_at2 = datetime.combine(second_day, time(12), moscow).astimezone(UTC)
    start = paid_at - timedelta(days=60)
    try:
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        merchant_owner, merchant, project, bot, _ = await _project(
            factory, _tid(),
        )
        async with admin_engine.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES (:id,'owner')"
                ), {"id": operator_tg})
                await conn.execute(text(
                    "UPDATE platform_saas_billing_control SET enabled=true,"
                    " effective_from=:effective WHERE id=1"
                ), {"effective": start - timedelta(days=5)})
                await conn.execute(text(
                    "UPDATE merchant_project_periods SET started_at=:start "
                    "WHERE project_id=:project AND ended_at IS NULL"
                ), {"start": start, "project": project})
                # Exact historical bot_connected event before paid_at. Current
                # event is later (the fixture registers the bot now).
                await conn.execute(text(
                    "INSERT INTO platform_usage_events "
                    "(project_id,event_type,source_id,bot_username,event_key,"
                    "occurred_at) VALUES(:project,'bot_connected',:bot,"
                    "'historical_test',:key,:happened)"
                ), {
                    "project": project, "bot": bot,
                    "key": "billing-history-" + str(uuid4()),
                    "happened": start,
                })
                await conn.execute(text(
                    "INSERT INTO merchant_platform_trial_claims "
                    "(id,merchant_id,owner_user_id,policy_key,scope,claimed_at)"
                    " VALUES(:id,:merchant,:owner,'vimi-saas-72h-v1',"
                    "'owner',:claimed)"
                ), {
                    "id": uuid4(), "merchant": merchant,
                    "owner": merchant_owner, "claimed": start,
                })

                bot_telegram = (await conn.execute(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:id"
                ), {"id": bot})).scalar_one()
                folder, product = uuid4(), uuid4()
                await conn.execute(text(
                    "INSERT INTO file_sale_folders(id,project_id,name,status)"
                    " VALUES(:id,:project,'Commission E2E','active')"
                ), {"id": folder, "project": project})
                await conn.execute(text(
                    "INSERT INTO file_sale_products "
                    "(id,project_id,folder_id,telegram_bot_id,name,status,"
                    "stars_price) VALUES(:id,:project,:folder,:bot,"
                    "'Commission E2E', 'published',200)"
                ), {
                    "id": product, "project": project,
                    "folder": folder, "bot": bot_telegram,
                })
                for amount, timestamp in (
                    ("50", paid_at),
                    ("30", paid_at2),
                ):
                    await conn.execute(text(
                        "INSERT INTO file_sale_manual_orders "
                        "(id,project_id,product_id,telegram_bot_id,"
                        "telegram_user_id,product_name,amount,currency_code,"
                        "payment_method_id,payment_method_name,instruction,"
                        "status,paid_at) VALUES "
                        "(:id,:project,:product,:bot,7171,'Commission E2E',"
                        ":amount,'USD',:method,'Bank','test',"
                        "'approved',:paid)"
                    ), {
                        "id": uuid4(), "project": project,
                        "product": product, "bot": bot_telegram,
                        "amount": amount, "method": uuid4(),
                        "paid": timestamp,
                    })
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(operator)})
                # First attempt must fail closed rather than charge zero or
                # pick a global/current rate.
                with pytest.raises(DBAPIError) as missing:
                    async with conn.begin_nested():
                        await _commission(conn, merchant, merchant_owner, month)
                assert getattr(missing.value.orig, "sqlstate", None) == "22023"

                await conn.execute(text(
                    "SELECT public.platform_saas_publish_fx_rate("
                    ":month,'USD',:rate)"
                ), {"month": month, "rate": Decimal("80")})
                for currency, rate in (
                    ("EUR", "90"), ("UAH", "2"), ("BYN", "27"),
                ):
                    await conn.execute(text(
                        "SELECT public.platform_saas_publish_fx_rate("
                        ":month,:currency,:rate)"
                    ), {
                        "month": month, "currency": currency,
                        "rate": Decimal(rate),
                    })
                with pytest.raises(DBAPIError) as override:
                    async with conn.begin_nested():
                        await _commission(
                            conn, merchant, merchant_owner, month, '{"USD":50}',
                        )
                assert getattr(override.value.orig, "sqlstate", None) == "22023"
                invoice = await _commission(conn, merchant, merchant_owner, month)
                assert invoice is not None
                assert await _commission(conn, merchant, merchant_owner, month) == invoice
                details = (await conn.execute(text(
                    "SELECT total_rub,status FROM platform_saas_invoices"
                    " WHERE id=:invoice"
                ), {"invoice": invoice})).one()
                assert details.status == "issued"
                assert Decimal(details.total_rub) == Decimal("320.00")
                lines = (await conn.execute(text(
                    "SELECT original_amount,rub_per_unit,charge_rub "
                    "FROM platform_saas_invoice_lines WHERE invoice_id=:id "
                    "ORDER BY original_amount DESC"
                ), {"id": invoice})).all()
                assert len(lines) == 2
                assert [(Decimal(q.original_amount), Decimal(q.rub_per_unit),
                         Decimal(q.charge_rub)) for q in lines] == [
                    (Decimal("50"), Decimal("80"), Decimal("200")),
                    (Decimal("30"), Decimal("80"), Decimal("120")),
                ]
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "UPDATE platform_saas_invoice_lines "
                            "SET rub_per_unit=100 WHERE invoice_id=:id"
                        ), {"id": invoice})
                # One USD/RUB reference total, convertible into any
                # administrator-selected settlement currency. No extra
                # invoice is created just by checking a quote.
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(merchant_owner)})
                for currency, expected in (
                    ("RUB", Decimal("470.00")),
                    ("USD", Decimal("5.88")),
                    ("EUR", Decimal("5.22")),
                    ("UAH", Decimal("235.00")),
                    ("BYN", Decimal("17.41")),
                ):
                    quote = (await conn.execute(text(
                        "SELECT public.platform_saas_quote_project_bill("
                        ":project,:month,:currency) AS data"
                    ).columns(data=JSONB), {
                        "project": project,
                        "month": month,
                        "currency": currency,
                    })).scalar_one()
                    assert quote["quote_ready"] is True
                    assert quote["chosen_currency"] == currency
                    assert Decimal(str(quote["total_rub"])) == Decimal("470")
                    assert Decimal(str(quote["estimated_total"])) == expected
                    assert quote["preview_only"] is True
                assert (await conn.execute(text(
                    "SELECT count(*) FROM platform_saas_invoices "
                    "WHERE kind='commission' AND merchant_id=:merchant"
                ), {"merchant": merchant})).scalar_one() == 1
                assert (await conn.execute(text(
                    "SELECT enabled FROM platform_saas_billing_control "
                    "WHERE id=1"
                ))).scalar_one() is True
            finally:
                # Never leave activated billing or an invoice in real DB:
                # CI's dedicated PostgreSQL transaction always rolls back.
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()
