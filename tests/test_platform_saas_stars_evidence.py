"""Real PostgreSQL: Stars USD reward snapshots are immutable, owner-only and OFF.

All inserted source purchases and snapshots roll back. No buyer access,
existing platform invoice, balance or commercial feature flag is modified.
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
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL")
    or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and privileged rollback-only tests",
)


async def _actor(conn, user):
    await conn.execute(
        text("SELECT set_config('app.current_user_id',:actor,true)"),
        {"actor": str(user)},
    )


async def test_file_stars_official_reward_and_idempotent_rated_evidence():
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
        paid_at = datetime.now(UTC)
        month = paid_at.astimezone(
            ZoneInfo("Europe/Moscow"),
        ).date().replace(day=1)
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": operator_tg})
                bot_tg = (await conn.execute(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:id"
                ), {"id": bot})).scalar_one()
                folder, product, purchase = uuid4(), uuid4(), uuid4()
                payload = "fs:" + uuid4().hex
                charge = "star-fixture-" + uuid4().hex
                await conn.execute(text(
                    "INSERT INTO file_sale_folders(id,project_id,name,status)"
                    " VALUES(:id,:project,'Stars audit','active')"
                ), {"id": folder, "project": project})
                await conn.execute(text(
                    "INSERT INTO file_sale_products "
                    "(id,project_id,folder_id,telegram_bot_id,name,status,stars_price)"
                    " VALUES(:id,:project,:folder,:bot,'Stars audit','published',2000)"
                ), {
                    "id": product, "project": project, "folder": folder,
                    "bot": bot_tg,
                })
                await conn.execute(text(
                    "INSERT INTO file_sale_purchases "
                    "(id,project_id,product_id,telegram_bot_id,"
                    "telegram_user_id,stars_amount,product_name,"
                    "invoice_payload,telegram_charge_id,status,paid_at)"
                    " VALUES(:id,:project,:product,:bot,7171,2000,"
                    "'Stars audit',:payload,:charge,'paid',:paid)"
                ), {
                    "id": purchase, "project": project, "product": product,
                    "bot": bot_tg, "payload": payload,
                    "charge": charge, "paid": paid_at,
                })
                await _actor(conn, owner)
                with pytest.raises(DBAPIError) as ordinary_cannot_capture:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_capture_new_file_stars(200)"
                        ))
                assert getattr(
                    ordinary_cannot_capture.value.orig, "sqlstate", None
                ) == "42501"
                await _actor(conn, operator)
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                stmt = text(
                    "SELECT public.platform_saas_capture_new_file_stars("
                    "200) AS data"
                ).columns(data=JSONB)
                captured = (await conn.execute(stmt)).scalar_one()
                # A shared CI database may also contain other genuine
                # paid Stars fixtures created by earlier tests in this job.
                # Collector is intentionally account-wide, not one project.
                assert captured["recorded"] >= 1
                assert captured["billing_activated"] is False
                assert (await conn.execute(stmt)).scalar_one()["recorded"] == 0
                with pytest.raises(DBAPIError) as table_denied:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT * FROM public.platform_saas_star_sales"
                        ))
                assert getattr(
                    table_denied.value.orig, "sqlstate", None
                ) == "42501"
                await _actor(conn, owner)
                view = (await conn.execute(text(
                    "SELECT public.platform_saas_star_project_summary("
                    ":project,:month) AS data"
                ).columns(data=JSONB), {
                    "project": project, "month": month,
                })).scalar_one()
                assert view["stars"] == 2000
                assert view["sold"] == 1
                assert view["pending_reconciliation"] == 0
                assert Decimal(view["reward_usd"]) == Decimal("26")
                assert Decimal(view["commission_usd"]) == Decimal("1.30")
                assert view["rows"][0]["price_stars"] == 2000
                await _actor(conn, operator)
                with pytest.raises(DBAPIError) as not_tenant:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_star_project_summary("
                            ":project,:month)"
                        ), {"project": project, "month": month})
                assert getattr(
                    not_tenant.value.orig, "sqlstate", None
                ) == "42501"
                await conn.execute(text("RESET ROLE"))
                result = (await conn.execute(text(
                    "SELECT source_kind,source_id,owner_user_id,merchant_id,"
                    "stars_amount,usd_per_star,reward_usd,commission_usd,"
                    "billing_eligible FROM platform_saas_star_sales "
                    "WHERE source_id=:id"
                ), {"id": purchase})).one()
                assert result.source_kind == "file_stars"
                assert result.owner_user_id == owner
                assert result.merchant_id == merchant
                assert result.stars_amount == 2000
                assert Decimal(result.usd_per_star) == Decimal("0.013")
                assert Decimal(result.reward_usd) == Decimal("26")
                assert Decimal(result.commission_usd) == Decimal("1.30")
                assert result.billing_eligible is False
                refund_id = uuid4()
                refund_sql = text(
                    "INSERT INTO public.file_sale_star_refunds "
                    "(id,project_id,purchase_id,telegram_bot_id,"
                    "telegram_user_id,telegram_charge_id,invoice_payload,"
                    "stars_amount,refunded_at) VALUES "
                    "(:id,:project,:purchase,:bot,7171,:charge,:payload,"
                    ":amount,:at)"
                )
                refund_args = {
                    "id": refund_id, "project": project,
                    "purchase": purchase, "bot": bot_tg,
                    "charge": charge, "payload": payload,
                    "amount": 2000, "at": paid_at,
                }
                with pytest.raises(DBAPIError) as invalid_refund:
                    async with conn.begin_nested():
                        await conn.execute(
                            refund_sql,
                            {**refund_args, "amount": 1999},
                        )
                assert getattr(
                    invalid_refund.value.orig, "sqlstate", None
                ) == "23514"
                await conn.execute(refund_sql, refund_args)
                # The original 0055 capture cutoff forbids backbilling old
                # Stars sales. A later refund of such a sale must stay in
                # Telegram history, NOT appear as an impossible pending fee.
                capture_cutoff = (await conn.execute(text(
                    "SELECT started_at FROM "
                    "platform_saas_star_capture_control WHERE id=1"
                ))).scalar_one()
                legacy_purchase = uuid4()
                legacy_payload = "fs:" + uuid4().hex
                legacy_charge = "old-charge-" + uuid4().hex
                await conn.execute(text(
                    "INSERT INTO file_sale_purchases "
                    "(id,project_id,product_id,telegram_bot_id,"
                    "telegram_user_id,stars_amount,product_name,"
                    "invoice_payload,telegram_charge_id,status,paid_at) "
                    "VALUES (:id,:project,:product,:bot,7171,2000,"
                    "'Stars old',:payload,:charge,'paid',:paid)"
                ), {
                    "id": legacy_purchase, "project": project,
                    "product": product, "bot": bot_tg,
                    "payload": legacy_payload, "charge": legacy_charge,
                    "paid": capture_cutoff - timedelta(days=1),
                })
                legacy_refund_id = uuid4()
                await conn.execute(refund_sql, {
                    "id": legacy_refund_id, "project": project,
                    "purchase": legacy_purchase, "bot": bot_tg,
                    "payload": legacy_payload, "charge": legacy_charge,
                    "amount": 2000, "at": paid_at,
                })
                await _actor(conn, operator)
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                refund_rpc = text(
                    "SELECT public.platform_saas_capture_star_refunds(200)"
                    " AS data"
                ).columns(data=JSONB)
                first = (await conn.execute(refund_rpc)).scalar_one()
                assert first["recorded"] >= 1
                assert first["applied_to_invoices"] is False
                assert (await conn.execute(refund_rpc)).scalar_one()["recorded"] == 0
                with pytest.raises(DBAPIError) as direct_refund_denied:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT * FROM platform_saas_star_refund_credits"
                        ))
                assert getattr(
                    direct_refund_denied.value.orig, "sqlstate", None
                ) == "42501"
                await _actor(conn, owner)
                history = (await conn.execute(text(
                    "SELECT public.platform_saas_star_project_refund_summary("
                    ":project,:month) AS data"
                ).columns(data=JSONB), {
                    "project": project, "month": month,
                })).scalar_one()
                assert history["refunds"] == 1
                assert history["refunded_stars"] == 2000
                assert Decimal(history["reward_credit_usd"]) == Decimal("26")
                assert Decimal(history["commission_credit_usd"]) == Decimal("1.30")
                assert history["pending_refund_reconciliation"] == 0
                await conn.execute(text("RESET ROLE"))
                assert (await conn.execute(text(
                    "SELECT count(*) FROM platform_saas_star_refund_credits "
                    "WHERE file_refund_id=:id"
                ), {"id": legacy_refund_id})).scalar_one() == 0
                assert (await conn.execute(text(
                    "SELECT status FROM file_sale_purchases WHERE id=:id"
                ), {"id": purchase})).scalar_one() == "paid"
                original_credit = (await conn.execute(text(
                    "SELECT commission_credit_usd "
                    "FROM platform_saas_star_refund_credits "
                    "WHERE file_refund_id=:id"
                ), {"id": refund_id})).scalar_one()
                assert Decimal(original_credit) == Decimal("1.30")
                with pytest.raises(DBAPIError) as immutable_credit:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "DELETE FROM platform_saas_star_refund_credits "
                            "WHERE file_refund_id=:id"
                        ), {"id": refund_id})
                assert getattr(
                    immutable_credit.value.orig, "sqlstate", None
                ) == "42501"
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "UPDATE platform_saas_star_sales "
                            "SET stars_amount=4000 WHERE source_id=:id"
                        ), {"id": purchase})
                assert (await conn.execute(text(
                    "SELECT enabled FROM platform_saas_billing_control WHERE id=1"
                ))).scalar_one() is False
            finally:
                await tx.rollback()
    finally:
        await admin.dispose()
        await engine.dispose()
