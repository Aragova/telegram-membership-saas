"""Rollback-only PostgreSQL contract for one-query central owner dashboard.

Checks independent bot heartbeat health, six-rail original currency revenue,
nearest real invoice, same-day sibling debts and cross-account isolation.
No synthetic sales or debts are committed.
"""
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated Postgres and non-bypass membership_runtime role required",
)


async def test_network_home_is_owner_scoped_and_shows_native_sales_and_due_bot():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin = create_async_engine(settings.migration_database_url)
    owner_tg = _tid()
    owner, merchant, project, bot_id, _ = await _project(factory, owner_tg)
    _, merchant2, project2, bot2, _ = await _project(factory, owner_tg)
    outsider, _, other_project, _, _ = await _project(factory, _tid())
    now = datetime.now(UTC)
    try:
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                telegram_id = (await conn.execute(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:id"
                ), {"id": bot_id})).scalar_one()
                await conn.execute(text(
                    "INSERT INTO customer_bot_runtime_health("
                    "project_id,telegram_bot_id,instance_id,state,last_heartbeat) "
                    "VALUES(:project,:tg,:instance,'running',:now) "
                    "ON CONFLICT(project_id) DO UPDATE SET "
                    "state='running',last_heartbeat=:now,"
                    "instance_id=:instance,telegram_bot_id=:tg"
                ), {"project": project, "tg": telegram_id,
                    "instance": bot_id, "now": now})
                folder, product = uuid4(), uuid4()
                await conn.execute(text(
                    "INSERT INTO file_sale_folders(id,project_id,name,status) "
                    "VALUES(:id,:project,'Fleet folder','active')"
                ), {"id": folder, "project": project})
                await conn.execute(text(
                    "INSERT INTO file_sale_products("
                    "id,project_id,folder_id,telegram_bot_id,name,stars_price,status) "
                    "VALUES(:id,:project,:folder,:tg,'Fleet file',42,'published')"
                ), {"id": product, "project": project,
                    "folder": folder, "tg": telegram_id})
                await conn.execute(text(
                    "INSERT INTO file_sale_manual_orders("
                    "id,project_id,product_id,telegram_bot_id,"
                    "telegram_user_id,product_name,amount,currency_code,"
                    "payment_method_id,payment_method_name,instruction,status,paid_at) "
                    "VALUES(:id,:project,:product,:tg,12345,'Manual file',"
                    "120,'RUB',:method,'Manual','test','approved',:now)"
                ), {"id": uuid4(), "project": project, "product": product,
                    "tg": telegram_id, "method": uuid4(), "now": now})
                await conn.execute(text(
                    "INSERT INTO file_sale_purchases("
                    "id,project_id,product_id,telegram_bot_id,telegram_user_id,"
                    "stars_amount,product_name,invoice_payload,"
                    "telegram_charge_id,status,paid_at)"
                    " VALUES(:id,:project,:product,:tg,12345,"
                    "42,'Stars file',:payload,:charge,'paid',:now)"
                ), {"id": uuid4(), "project": project, "product": product,
                    "tg": telegram_id, "payload": str(uuid4()),
                    "charge": str(uuid4()), "now": now})
                app_id = uuid4()
                await conn.execute(text(
                    "INSERT INTO crypto_pay_apps(id,project_id,telegram_bot_id,"
                    "owner_user_id,token_ciphertext,environment,app_label,status) "
                    "VALUES(:id,:project,:tg,:owner,'test','mainnet',"
                    "'Fleet Crypto','active')"
                ), {"id": app_id, "project": project, "tg": telegram_id,
                    "owner": owner})
                await conn.execute(text(
                    "INSERT INTO crypto_pay_invoices("
                    "id,project_id,app_id,telegram_bot_id,telegram_user_id,"
                    "kind,product_id,name_snapshot,amount,currency_code,"
                    "currency_type,payload,provider_invoice_id,status,paid_at) "
                    "VALUES(:id,:project,:app,:tg,12345,'file',:product,"
                    "'Unvalued TON',0.75,'TON','crypto',:payload,"
                    "98765432,'paid',:now)"
                ), {"id": uuid4(), "project": project, "app": app_id,
                    "tg": telegram_id, "product": product,
                    "payload": str(uuid4()), "now": now})

                first = await _invoice(conn, owner, merchant, bot_id)
                second = await _invoice(conn, owner, merchant2, bot2)
                for project_id, bot, invoice in (
                    (project, bot_id, first), (project2, bot2, second),
                ):
                    await _line(conn, invoice, project_id, "bot_fee", bot)
                    await conn.execute(text(
                        "UPDATE platform_saas_invoices SET status='issued' "
                        "WHERE id=:invoice"
                    ), {"invoice": invoice})

                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:owner,true)"
                ), {"owner": str(owner)})
                result = (await conn.execute(text(
                    "SELECT public.platform_saas_owner_network_home() AS report"
                ).columns(report=JSONB))).scalar_one()
                assert result["network"] == {
                    "connected": 2, "healthy": 1,
                    "attention": 0, "down": 1,
                }
                money = {i["currency"]: Decimal(i["gross"])
                         for i in result["revenue"]}
                assert money == {"RUB": Decimal("120"),
                                 "XTR": Decimal("42"),
                                 "TON": Decimal("0.75")}
                assert result["billing"]["unpaid"] == 2
                assert result["billing"]["overdue"] == 0
                upcoming = result["billing"]["next"]
                assert upcoming["username"] == "billing_test_bot"
                assert UUID(upcoming["invoice_id"]) in (first, second)
                assert upcoming["same_day_count"] == 2
                assert Decimal(upcoming["remaining_rub"]) == 150
                # Resolve the current page for the exact issued invoice.
                page = (await conn.execute(text(
                    "SELECT public.platform_saas_owner_invoice_page(:invoice)"
                ), {"invoice": UUID(upcoming["invoice_id"])})).scalar_one()
                assert page == 0

                # Outsider gets only their own one disconnected-from-runtime
                # project, not the first owner's money or invoice references.
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:owner,true)"
                ), {"owner": str(outsider)})
                stranger = (await conn.execute(text(
                    "SELECT public.platform_saas_owner_network_home() AS report"
                ).columns(report=JSONB))).scalar_one()
                assert stranger["network"]["connected"] == 1
                assert stranger["revenue"] == []
                assert stranger["billing"]["next"] is None
                assert stranger["billing"]["unpaid"] == 0
                assert other_project != project
                with pytest.raises(DBAPIError) as denied_other_invoice:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_owner_invoice_page(:invoice)"
                        ), {"invoice": first})
                assert getattr(
                    denied_other_invoice.value.orig, "sqlstate", None,
                ) == "42501"

                await conn.execute(text(
                    "SELECT set_config('app.current_user_id','',true)"
                ))
                with pytest.raises(DBAPIError) as denied:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_owner_network_home()"
                        ))
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                await conn.execute(text("RESET ROLE"))
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()
