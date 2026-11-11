"""True database RLS / financial preview of the connected-bot Bill.

All source/FX writes happen inside one privileged rollback-only test transaction.
The Bill preview never creates invoices or enables monetization.
"""
import os
from datetime import UTC, datetime, time
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

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires PostgreSQL under nonbypass runtime role",
)


async def _preview(conn, project, month):
    return (await conn.execute(
        text(
            "SELECT public.platform_saas_project_preview("
            ":project,:month) AS report"
        ).columns(report=JSONB),
        {"project": project, "month": month},
    )).scalar_one()


async def _actor(conn, user):
    await conn.execute(
        text("SELECT set_config('app.current_user_id',:id,true)"),
        {"id": str(user)},
    )


async def test_bill_preview_groups_fiat_files_stars_per_project_and_blocks_other_owners():
    config = Settings()
    engine = build_engine(config)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(config.migration_database_url)
    moscow = ZoneInfo("Europe/Moscow")
    local_date = datetime.now(UTC).astimezone(moscow).date()
    paid_at = datetime.combine(local_date, time(12), tzinfo=moscow).astimezone(UTC)
    month = local_date.replace(day=1)
    owner_tg = _tid()
    try:
        owner, merchant, project, bot_uuid, _ = await _project(factory, owner_tg)
        outsider, other_merchant, other_project, _, _ = await _project(
            factory, _tid(),
        )
        assert merchant != other_merchant
        async with admin_engine.connect() as conn:
            tx = await conn.begin()
            try:
                bot_telegram_id = (await conn.execute(
                    text("SELECT telegram_bot_id FROM bot_instances WHERE id=:id"),
                    {"id": bot_uuid},
                )).scalar_one()
                folder, file, sale, star = (
                    uuid4(), uuid4(), uuid4(), uuid4(),
                )
                await conn.execute(text(
                    "INSERT INTO file_sale_folders "
                    "(id,project_id,name,status) "
                    "VALUES (:id,:project,'Financial preview','active')"
                ), {"id": folder, "project": project})
                await conn.execute(text(
                    "INSERT INTO file_sale_products "
                    "(id,project_id,folder_id,telegram_bot_id,name,"
                    "stars_price,status) VALUES "
                    "(:id,:project,:folder,:bot,'Preview file',200,'published')"
                ), {
                    "id": file, "project": project,
                    "folder": folder, "bot": bot_telegram_id,
                })
                await conn.execute(text(
                    "INSERT INTO file_sale_manual_orders "
                    "(id,project_id,product_id,telegram_bot_id,"
                    "telegram_user_id,product_name,amount,currency_code,"
                    "payment_method_id,payment_method_name,instruction,status,"
                    "paid_at) VALUES "
                    "(:id,:project,:product,:bot,123987,'Preview file',"
                    "50,'USD',:method,'Bank','Test',"
                    "'approved',:paid)"
                ), {
                    "id": sale, "project": project, "product": file,
                    "bot": bot_telegram_id, "method": uuid4(),
                    "paid": paid_at,
                })
                await conn.execute(text(
                    "INSERT INTO file_sale_purchases "
                    "(id,project_id,product_id,telegram_bot_id,"
                    "telegram_user_id,stars_amount,product_name,"
                    "invoice_payload,telegram_charge_id,status,paid_at) "
                    "VALUES(:id,:project,:product,:bot,123987,200,"
                    "'Preview file',:payload,:charge,'paid',:paid)"
                ), {
                    "id": star, "project": project, "product": file,
                    "bot": bot_telegram_id, "payload": str(star),
                    "charge": "charge-" + str(star), "paid": paid_at,
                })
                # Real configured platform operator publishes rates. A
                # non-owner can view only their own project report.
                await conn.execute(text(
                    "INSERT INTO platform_operators "
                    "(telegram_user_id,role) VALUES(:id,'owner')"
                ), {"id": owner_tg})
                await _actor(conn, owner)
                for cur, rate in (("USD", "80"), ("XTR", "1.04")):
                    await conn.execute(text(
                        "SELECT public.platform_saas_publish_fx_rate("
                        ":day,:currency,:rate)"
                    ), {
                        "day": month, "currency": cur,
                        "rate": Decimal(rate),
                    })
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                try:
                    own = await _preview(conn, project, month)
                    assert own["is_platform_owner"] is True
                    assert own["next_bot_fee_rub"] == 0
                    assert own["fiat_commission_rub"] == 0
                    rows = {r["currency"]: r for r in own["rows"]}
                    assert Decimal(str(rows["USD"]["gross"])) == 50
                    assert Decimal(str(rows["USD"]["fee_rub"])) == 200
                    assert Decimal(str(rows["XTR"]["fee_rub"])) == Decimal("10.4")
                    assert own["stars_need_reconciliation"] is True
                    assert own["estimate_complete"] is False
                    assert own["missing_monthly_rates"] == 0
                    await _actor(conn, outsider)
                    with pytest.raises(DBAPIError) as denied:
                        async with conn.begin_nested():
                            await _preview(conn, project, month)
                    assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                    stranger = await _preview(conn, other_project, month)
                    assert stranger["rows"] == []
                    assert stranger["next_bot_fee_rub"] == 150
                finally:
                    await conn.execute(text("RESET ROLE"))
                assert (await conn.execute(text(
                    "SELECT enabled FROM platform_saas_billing_control "
                    "WHERE id=1"
                ))).scalar_one() is False
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()
