"""Real PostgreSQL proof: verified Stars refunds offset only real SaaS debt.

No network calls, test-only Telegram screens, or financial production writes.
Every paid source, receipt and immutable allocation is rolled back.
"""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _receipt, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated PostgreSQL owner transaction required",
)


async def _json(conn, sql, **params):
    return (await conn.execute(
        text("SELECT " + sql + " AS data").columns(data=JSONB), params,
    )).scalar_one()


@pytest.mark.parametrize(
    "payment_state", ["not_paid", "confirmed", "proof_pending"]
)
async def test_verified_star_refund_applies_once_and_carries_after_payment(
    payment_state: str,
):
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    try:
        operator_tg = _tid()
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        owner, merchant, project, bot, _ = await _project(factory, _tid())
        now = datetime.now(UTC)
        paid_at = now + timedelta(seconds=10)
        refunded_at = paid_at + timedelta(seconds=30)
        sale_day = paid_at.astimezone(ZoneInfo("Europe/Moscow")).date()
        folder, product, purchase, star_sale, refund = (
            uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
        )
        charge_id, payload = "stars-" + uuid4().hex, "fs:" + uuid4().hex
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:id,'owner')"
                ), {"id": operator_tg})
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(operator)})
                bot_tg = (await conn.execute(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:id"
                ), {"id": bot})).scalar_one()
                await conn.execute(text(
                    "INSERT INTO platform_saas_daily_fx_rates "
                    "(rate_date,currency_code,rub_per_unit,source_name,"
                    "source_date,published_by) "
                    "VALUES(:day,'USD',90,'CBR',:day,:actor) "
                    "ON CONFLICT DO NOTHING"
                ), {"day": sale_day, "actor": operator})
                await conn.execute(text(
                    "INSERT INTO file_sale_folders(id,project_id,name,status)"
                    " VALUES(:id,:project,'Refund ledger','active')"
                ), {"id": folder, "project": project})
                await conn.execute(text(
                    "INSERT INTO file_sale_products "
                    "(id,project_id,folder_id,telegram_bot_id,name,status,"
                    "stars_price) VALUES(:id,:project,:folder,:bot,"
                    "'Refund ledger','published',2000)"
                ), {"id": product, "project": project,
                    "folder": folder, "bot": bot_tg})
                await conn.execute(text(
                    "INSERT INTO file_sale_purchases "
                    "(id,project_id,product_id,telegram_bot_id,telegram_user_id,"
                    "stars_amount,product_name,invoice_payload,"
                    "telegram_charge_id,status,paid_at) "
                    "VALUES(:id,:project,:product,:bot,7171,2000,"
                    "'Refund ledger',:payload,:charge,'paid',:paid)"
                ), {
                    "id": purchase, "project": project, "product": product,
                    "bot": bot_tg, "payload": payload, "charge": charge_id,
                    "paid": paid_at,
                })
                await conn.execute(text(
                    "INSERT INTO platform_saas_star_sales("
                    "id,source_kind,source_id,project_id,merchant_id,"
                    "owner_user_id,telegram_bot_id,telegram_charge_id,"
                    "stars_amount,usd_per_star,reward_usd,commission_usd,"
                    "rate_effective_on,paid_at,billing_eligible) "
                    "VALUES(:id,'file_stars',:source,:project,:merchant,"
                    ":owner,:bot,:charge,2000,0.013,26,1.3,"
                    "(SELECT max(effective_on) FROM "
                    "platform_saas_star_reward_rates WHERE effective_on<=:day),"
                    ":paid,true)"
                ), {
                    "id": star_sale, "source": purchase,
                    "project": project, "merchant": merchant, "owner": owner,
                    "bot": bot_tg, "charge": charge_id,
                    "day": sale_day, "paid": paid_at,
                })
                commission = await _invoice(
                    conn, owner, merchant, bot, kind="commission",
                    total="117", period_start=paid_at-timedelta(hours=1),
                )
                line = uuid4()
                await conn.execute(text(
                    "INSERT INTO platform_saas_invoice_lines "
                    "(id,invoice_id,project_id,source_kind,source_id,"
                    "bot_instance_id,original_amount,original_currency,"
                    "rub_per_unit,fee_rate,charge_rub) "
                    "VALUES(:line,:invoice,:project,'file_stars',:purchase,"
                    ":bot,2000,'XTR',1.17,0.05,117)"
                ), {
                    "line": line, "invoice": commission, "project": project,
                    "purchase": purchase, "bot": bot,
                })
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued'"
                    " WHERE id=:invoice"
                ), {"invoice": commission})
                other_invoice = None
                if payment_state == "confirmed":
                    other_invoice = await _invoice(
                        conn, owner, merchant, bot, total="150",
                        period_start=now+timedelta(minutes=1),
                    )
                    await _line(
                        conn, other_invoice, project, "bot_fee", bot,
                    )
                    await conn.execute(text(
                        "UPDATE platform_saas_invoices SET status='issued'"
                        " WHERE id=:invoice"
                    ), {"invoice": other_invoice})
                    await _receipt(conn, commission, operator, "117")
                pending_attempt = None
                if payment_state == "proof_pending":
                    pending_attempt = uuid4()
                    day = now.astimezone(ZoneInfo("Europe/Moscow")).date()
                    await conn.execute(text(
                        "INSERT INTO platform_saas_payment_attempts_v2 "
                        "(id,invoice_id,owner_user_id,amount_rub,currency_code,"
                        "rub_per_unit,amount_currency,quote_date,"
                        "instructions_snapshot,expires_at) "
                        "VALUES(:id,:invoice,:owner,117,'RUB',1,117,:day,"
                        "'Real RUB bank instructions',:expiry)"
                    ), {
                        "id": pending_attempt, "invoice": commission,
                        "owner": owner, "day": day,
                        "expiry": now+timedelta(hours=1),
                    })
                    await conn.execute(text(
                        "INSERT INTO platform_saas_payment_proofs_v2("
                        "id,attempt_id,source_bot_id,source_chat_id,"
                        "source_message_id,source_file_id,file_type)"
                        " VALUES(:id,:attempt,12345,67890,321,"
                        "'test_proof_document','document')"
                    ), {"id": uuid4(), "attempt": pending_attempt})
                await conn.execute(text(
                    "INSERT INTO file_sale_star_refunds("
                    "id,project_id,purchase_id,telegram_bot_id,telegram_user_id,"
                    "telegram_charge_id,invoice_payload,stars_amount,refunded_at)"
                    " VALUES(:id,:project,:purchase,:bot,7171,:charge,"
                    ":payload,2000,:refunded)"
                ), {
                    "id": refund, "project": project, "purchase": purchase,
                    "bot": bot_tg, "charge": charge_id, "payload": payload,
                    "refunded": refunded_at,
                })
                captured = await _json(
                    conn, "public.platform_saas_capture_star_refunds(50)"
                )
                assert captured["recorded"] >= 1
                first = await _json(
                    conn, "public.platform_saas_reconcile_verified_refunds(:owner)",
                    owner=owner,
                )
                assert first["claimed"] == 1
                assert first["allocated"] == (
                    0 if payment_state == "proof_pending" else 1
                )
                second = await _json(
                    conn, "public.platform_saas_reconcile_verified_refunds(:owner)",
                    owner=owner,
                )
                assert second == {"claimed": 0, "allocated": 0}
                amounts = (await conn.execute(text(
                    "SELECT credit_rub FROM platform_saas_verified_credit_claims"
                    " WHERE source_kind='stars_file' AND source_credit_id IN"
                    " (SELECT id FROM platform_saas_star_refund_credits "
                    " WHERE original_sale_id=:sale)"
                ), {"sale": star_sale})).scalars().all()
                assert amounts == [Decimal("117.00")]
                if payment_state == "proof_pending":
                    # A previously transferred full 117 RUB remains
                    # approvable after the verified refund is recorded.
                    approved = (await conn.execute(text(
                        "SELECT public.platform_saas_v2_approve_attempt("
                        ":attempt,:reference)"
                    ), {
                        "attempt": pending_attempt,
                        "reference": "pending-bank-" + uuid4().hex,
                    })).scalar_one()
                    assert approved is not None
                    other_invoice = await _invoice(
                        conn, owner, merchant, bot, total="150",
                        period_start=now+timedelta(minutes=2),
                    )
                    await _line(conn, other_invoice, project, "bot_fee", bot)
                    await conn.execute(text(
                        "UPDATE platform_saas_invoices SET status='issued'"
                        " WHERE id=:invoice"
                    ), {"invoice": other_invoice})
                    carried = await _json(
                        conn, "public.platform_saas_reconcile_verified_refunds(:owner)",
                        owner=owner,
                    )
                    assert carried == {"claimed": 0, "allocated": 1}
                applied_invoice = other_invoice or commission
                credit_balance = await _json(
                    conn, "public.platform_saas_v2_invoice_balance(:invoice)",
                    invoice=applied_invoice,
                )
                assert Decimal(credit_balance["verified_refund_credit_rub"]) == 117
                assert Decimal(credit_balance["remaining_rub"]) == (
                    33 if payment_state != "not_paid" else 0
                )
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "DELETE FROM platform_saas_verified_credit_claims"
                            " WHERE source_credit_id=(SELECT id FROM "
                            " platform_saas_star_refund_credits"
                            " WHERE original_sale_id=:sale)"
                        ), {"sale": star_sale})
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()
