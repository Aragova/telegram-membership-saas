"""ViMi Pay multi-rail month report, migrated PostgreSQL/RLS, no billing.

Financial test mutations run inside a privileged rollback-only transaction.
"""
import os
from datetime import UTC, datetime
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
from telegram_membership_saas.modules.platform_saas_invoice_ui import (
    unified_monthly_classic,
    unified_monthly_rich,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated PostgreSQL and membership_runtime RLS required",
)


async def _preview(conn, owner, month, project=None):
    return (await conn.execute(text(
        "SELECT public.platform_saas_unified_monthly_preview("
        ":owner,:month,:project) AS data"
    ).columns(data=JSONB), {
        "owner": owner, "month": month, "project": project,
    })).scalar_one()


async def test_unified_month_is_owner_scoped_and_nonbillable_when_disabled():
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    owner, _, project, bot_id, _ = await _project(factory, _tid())
    stranger, _, other, _, _ = await _project(factory, _tid())
    now = datetime.now(UTC)
    month = now.astimezone(ZoneInfo("Europe/Moscow")).date().replace(day=1)
    try:
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                bot_telegram_id = (await conn.execute(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:id"
                ), {"id": bot_id})).scalar_one()
                folder, product, sale = uuid4(), uuid4(), uuid4()
                await conn.execute(text(
                    "INSERT INTO file_sale_folders(id,project_id,name,status)"
                    " VALUES(:id,:p,'Month audit','active')"
                ), {"id": folder, "p": project})
                await conn.execute(text(
                    "INSERT INTO file_sale_products("
                    "id,project_id,folder_id,telegram_bot_id,name,status)"
                    " VALUES(:id,:p,:folder,:bot,'File','published')"
                ), {
                    "id": product, "p": project,
                    "folder": folder, "bot": bot_telegram_id,
                })
                await conn.execute(text(
                    "INSERT INTO file_sale_manual_orders("
                    "id,project_id,product_id,telegram_bot_id,"
                    "telegram_user_id,product_name,amount,currency_code,"
                    "payment_method_id,payment_method_name,instruction,status,"
                    "paid_at) VALUES(:id,:p,:product,:bot,1234567,'File',"
                    "120,'RUB',:method,'Bank','Test','approved',:now)"
                ), {
                    "id": sale, "p": project,
                    "product": product, "bot": bot_telegram_id,
                    "method": uuid4(), "now": now,
                })
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:id,true)"
                ), {"id": str(owner)})
                summary = await _preview(conn, owner, month, project)
                assert summary["manual_sales"] == 1
                assert summary["stars_sales"] == 0
                assert summary["crypto_sales"] == 0
                assert summary["unrated_billable"] == 0
                assert summary["billing_enabled"] is False
                assert summary["preview_only"] is True
                assert Decimal(summary["commission_rub"]) == 0
                assert any(row["kind"] == "file_manual" and row["sold"] == 1
                           for row in summary["rails"])
                assert "ViMi Pay" in unified_monthly_rich(summary)
                assert "ViMi Pay" in unified_monthly_classic(summary)
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:id,true)"
                ), {"id": str(stranger)})
                with pytest.raises(DBAPIError) as forbidden:
                    async with conn.begin_nested():
                        await _preview(conn, owner, month, project)
                assert getattr(forbidden.value.orig, "sqlstate", None) == "42501"
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await _preview(conn, stranger, month, project)
                own = await _preview(conn, stranger, month, other)
                assert own["manual_sales"] == 0
                assert own["commission_rub"] == "0.00"
                await conn.execute(text("RESET ROLE"))
                assert (await conn.execute(text(
                    "SELECT enabled FROM platform_saas_billing_control WHERE id=1"
                ))).scalar_one() is False
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()


async def test_unified_owner_month_rejects_nonmonth_and_missing_identity():
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    try:
        owner, _, project, _, _ = await _project(factory, _tid())
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(owner)})
                with pytest.raises(DBAPIError) as error:
                    async with conn.begin_nested():
                        await _preview(conn, owner, datetime.now(UTC).date(), project)
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await _preview(conn, owner, None, project)
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()
