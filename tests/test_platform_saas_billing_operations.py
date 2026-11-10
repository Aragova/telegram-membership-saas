"""Real PostgreSQL tests for owner-only, opt-in SaaS billing RPCs.

All writes run under a privileged rollback-only transaction. No real invoices,
billing activation, trial claims or confirmation receipts survive the test.
"""
import calendar
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_billing_service import (
    platform_saas_invoice_report,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires real PostgreSQL migration and non-bypass runtime role",
)


async def _actor(admin, user_id):
    await admin.execute(text(
        "SELECT set_config('app.current_user_id',:id,true)"
    ), {"id": str(user_id)})


def _month_after(start):
    month = 1 if start.month == 12 else start.month + 1
    year = start.year + int(start.month == 12)
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


async def test_disabled_billing_denies_invoice_writes_and_tenant_report():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    operator_tg = _tid()
    try:
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        merchant, _, _, bot, _ = await _project(factory, _tid())
        start = datetime.now(UTC) + timedelta(minutes=10)
        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                await admin.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES (:id,'owner')"
                ), {"id": operator_tg})
                await _actor(admin, operator)
                state = (await admin.execute(text(
                    "SELECT public.platform_saas_invoice_report(0,6) AS snapshot"
                ).columns(snapshot=JSONB))).scalar_one()
                assert state["enabled"] is False
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "SELECT public.platform_saas_issue_bot_invoice("
                            ":bot,:start,:end)"
                        ), {
                            "bot": bot, "start": start, "end": _month_after(start),
                        })
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "SELECT public.platform_saas_issue_commission_invoice("
                            ":merchant,:owner,:month,CAST('{}' AS jsonb))"
                        ), {
                            "merchant": uuid4(), "owner": merchant,
                            "month": start.date().replace(day=1),
                        })
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
                await _actor(admin, merchant)
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "SELECT public.platform_saas_invoice_report(0,6)"
                        ))
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
            finally:
                await tx.rollback()

        with pytest.raises(PermissionError):
            await platform_saas_invoice_report(
                factory, actor_user_id=merchant, actor_telegram_id=_tid(),
                settings=Settings(platform_owner_telegram_id=operator_tg),
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=merchant),
        ) as session:
            # FORCE RLS with no tenant policy may silently affect zero rows.
            try:
                result = await session.execute(text(
                    "UPDATE platform_saas_billing_control SET enabled=true "
                    "WHERE id=1 RETURNING id"
                ))
            except DBAPIError:
                pass
            else:
                assert result.fetchall() == []
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_opt_in_owner_bot_invoice_receipt_idempotent_and_self_exempt():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    operator_tg = _tid()
    try:
        operator, own_merchant, own_project, own_bot, _ = (
            await _project(factory, operator_tg)
        )
        merchant, merchant_id, project, bot, _ = await _project(factory, _tid())
        now = datetime.now(UTC)
        start = now + timedelta(minutes=10)
        end = _month_after(start)
        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                await admin.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES (:id,'owner')"
                ), {"id": operator_tg})
                await admin.execute(text(
                    "UPDATE platform_saas_billing_control SET "
                    "enabled=true,effective_from=:start WHERE id=1"
                ), {"start": now - timedelta(days=1)})
                await admin.execute(text("""
                    INSERT INTO merchant_platform_trial_claims(
                        id,merchant_id,owner_user_id,policy_key,scope,claimed_at
                    ) VALUES (:id,:merchant,:owner,'vimi-saas-72h-v1',
                              'owner',:claimed)
                """), {
                    "id": uuid4(), "merchant": merchant_id,
                    "owner": merchant, "claimed": now - timedelta(days=10),
                })
                await _actor(admin, operator)
                own = (await admin.execute(text(
                    "SELECT public.platform_saas_issue_bot_invoice("
                    ":bot,:start,:end)"
                ), {
                    "bot": own_bot, "start": start, "end": end,
                })).scalar_one()
                assert own is None
                assert own_merchant != merchant_id
                assert own_project != project
                invoice = (await admin.execute(text(
                    "SELECT public.platform_saas_issue_bot_invoice("
                    ":bot,:start,:end)"
                ), {
                    "bot": bot, "start": start, "end": end,
                })).scalar_one()
                assert invoice is not None
                repeated = (await admin.execute(text(
                    "SELECT public.platform_saas_issue_bot_invoice("
                    ":bot,:start,:end)"
                ), {
                    "bot": bot, "start": start, "end": end,
                })).scalar_one()
                assert repeated == invoice
                statement = (await admin.execute(text(
                    "SELECT total_rub,status FROM platform_saas_invoices "
                    "WHERE id=:id"
                ), {"id": invoice})).one()
                assert statement.total_rub == Decimal("150.00")
                assert statement.status == "issued"
                (line_count,) = (await admin.execute(text(
                    "SELECT count(*) FROM platform_saas_invoice_lines "
                    "WHERE invoice_id=:id"
                ), {"id": invoice})).one()
                assert line_count == 1

                # Even after enabling billing, the ordinary tenant cannot
                # authorize a receipt via the SECURITY DEFINER RPC.
                await _actor(admin, merchant)
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "SELECT public.platform_saas_confirm_receipt(:id,NULL)"
                        ), {"id": invoice})
                assert getattr(error.value.orig, "sqlstate", None) == "42501"

                await _actor(admin, operator)
                receipt = (await admin.execute(text(
                    "SELECT public.platform_saas_confirm_receipt("
                    ":id,'bank-confirmed-1')"
                ), {"id": invoice})).scalar_one()
                again = (await admin.execute(text(
                    "SELECT public.platform_saas_confirm_receipt("
                    ":id,'bank-confirmed-1')"
                ), {"id": invoice})).scalar_one()
                assert receipt == again
                report = (await admin.execute(text(
                    "SELECT public.platform_saas_invoice_report(0,6)"
                ))).scalar_one()
                assert report["unpaid"] == 0
                assert Decimal(report["received_rub"]) == Decimal("150.00")
                assert report["invoices"][0]["status"] == "paid"

                with pytest.raises(DBAPIError):
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "SELECT public.platform_saas_issue_bot_invoice("
                            ":bot,:start,:end)"
                        ), {
                            "bot": bot, "start": start + timedelta(days=1),
                            "end": _month_after(start + timedelta(days=1)),
                        })
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()
