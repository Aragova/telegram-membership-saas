"""Rollback-only E2E for verified per-bot source and anniversary billing.

No real payments, Telegram messages, staging bots or lasting DB mutations.
"""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires the migrated CI PostgreSQL and non-bypass membership role",
)


async def test_only_original_bots_verified_file_sales_enter_its_renewal():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    admin = create_async_engine(Settings().migration_database_url)
    try:
        owner, merchant, project, bot, _ = await _project(factory, _tid())
        other_owner, other_merchant, other_project, other_bot, _ = (
            await _project(factory, _tid())
        )
        now = datetime.now(UTC)
        start = now - timedelta(days=10)
        paid_at = now - timedelta(days=2)
        activated = now - timedelta(days=30)
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "UPDATE platform_saas_billing_control "
                    "SET enabled=true,effective_from=:date WHERE id=1"
                ), {"date": activated})
                await conn.execute(text(
                    "INSERT INTO platform_saas_release_audit "
                    "(id,actor_id,action,registered_bots,nonoperator_owners,"
                    " effective_from,created_at) "
                    "VALUES(:id,:actor,'arm',2,2,:date,:created)"
                ), {
                    "id": uuid4(), "actor": owner, "date": activated,
                    "created": activated - timedelta(minutes=5),
                })
                await conn.execute(text(
                    "INSERT INTO merchant_platform_trial_claims "
                    "(id,merchant_id,owner_user_id,policy_key,scope,claimed_at) "
                    "VALUES(:id,:merchant,:owner,'vimi-saas-72h-v1',"
                    "'owner',:claimed)"
                ), {
                    "id": uuid4(), "merchant": merchant, "owner": owner,
                    "claimed": activated - timedelta(days=7),
                })
                await conn.execute(text(
                    "UPDATE merchant_project_periods SET started_at=:date "
                    "WHERE project_id=:project AND ended_at IS NULL"
                ), {
                    "date": activated - timedelta(days=2),
                    "project": project,
                })
                await conn.execute(text(
                    "INSERT INTO platform_usage_events "
                    "(project_id,event_type,source_id,bot_username,event_key,"
                    " occurred_at) VALUES "
                    "(:project,'bot_connected',:bot,'original_test_bot',"
                    ":key,:date)"
                ), {
                    "project": project, "bot": bot,
                    "key": "bot-billing-" + str(uuid4()),
                    "date": activated - timedelta(days=1),
                })
                bot_tg = (await conn.execute(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:bot"
                ), {"bot": bot})).scalar_one()
                folder, product = uuid4(), uuid4()
                await conn.execute(text(
                    "INSERT INTO file_sale_folders(id,project_id,name,status) "
                    "VALUES(:id,:project,'Renewal fixture','active')"
                ), {"id": folder, "project": project})
                await conn.execute(text(
                    "INSERT INTO file_sale_products "
                    "(id,project_id,folder_id,telegram_bot_id,name,status,stars_price)"
                    " VALUES(:id,:project,:folder,:bot,'Renewal fixture',"
                    "'published',200)"
                ), {
                    "id": product, "project": project, "folder": folder,
                    "bot": bot_tg,
                })
                sale_id = uuid4()
                await conn.execute(text(
                    "INSERT INTO file_sale_manual_orders "
                    "(id,project_id,product_id,telegram_bot_id,telegram_user_id,"
                    "product_name,amount,currency_code,payment_method_id,"
                    "payment_method_name,instruction,status,paid_at)"
                    " VALUES(:id,:project,:product,:bot,7171,'Renewal fixture',"
                    "4000,'RUB',:method,'Bank','test','approved',:paid)"
                ), {
                    "id": sale_id, "project": project,
                    "product": product, "bot": bot_tg,
                    "method": uuid4(), "paid": paid_at,
                })
                result = (await conn.execute(
                    text("SELECT public.platform_saas_bot_previous_sales("
                         ":bot,:start,:end,:owner,:merchant) AS data")
                        .columns(data=JSONB),
                    {
                        "bot": bot, "start": start, "end": now,
                        "owner": owner, "merchant": merchant,
                    },
                )).scalar_one()
                assert len(result) == 1
                line = result[0]
                assert line["kind"] == "file_manual"
                assert line["id"] == str(sale_id)
                assert Decimal(str(line["charge"])) == Decimal("200")
                assert Decimal(str(line["amount"])) == Decimal("4000")
                assert line["currency"] == "RUB"
                # A different bot/merchant must not inherit any sale.
                unrelated = (await conn.execute(
                    text("SELECT public.platform_saas_bot_previous_sales("
                         ":bot,:start,:end,:owner,:merchant) AS data")
                        .columns(data=JSONB),
                    {
                        "bot": other_bot, "start": start, "end": now,
                        "owner": other_owner, "merchant": other_merchant,
                    },
                )).scalar_one()
                assert unrelated == []
                # The high-privilege sales helper must not be executable by
                # the regular membership runtime role.
                assert (await conn.execute(text(
                    "SELECT has_function_privilege("
                    "'membership_runtime',"
                    "'public.platform_saas_bot_previous_sales("
                    "uuid,timestamptz,timestamptz,uuid,uuid)',"
                    "'EXECUTE')"
                ))).scalar_one() is False
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()
