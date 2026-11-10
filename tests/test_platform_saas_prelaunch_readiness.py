"""Read-only prelaunch exemption inventory, tested on disposable CI PostgreSQL.

No live billing: the privileged verification row is rolled back with the test.
"""
import os
from uuid import uuid4

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
    reason="Requires migrated PostgreSQL and privileged rollback-only audit",
)


async def _identity(admin, user_id):
    await admin.execute(text(
        "SELECT set_config('app.current_user_id',:value,true)"
    ), {"value": str(user_id)})


async def _report(admin):
    return (await admin.execute(
        text("SELECT public.platform_saas_exemption_preflight() AS data")
        .columns(data=JSONB),
    )).scalar_one()


async def _page(admin, page, size):
    return (await admin.execute(
        text(
            "SELECT public.platform_saas_exemption_preflight_page("
            ":page,:size) AS data"
        ).columns(data=JSONB),
        {"page": page, "size": size},
    )).scalar_one()


async def test_prelaunch_report_is_read_only_owner_scoped_and_exemption_specific():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    operator_tg = _tid()
    try:
        owner, merchant, project, verified_bot, chat = await _project(
            factory, operator_tg,
        )
        _, _, _, unverified_bot, _ = await _project(factory, operator_tg)
        other_owner, _, _, outsider_bot, _ = await _project(factory, _tid())
        assert verified_bot != unverified_bot
        assert verified_bot != outsider_bot

        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                await admin.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES (:tg,'owner')"
                ), {"tg": operator_tg})
                await _identity(admin, owner)

                before = await _report(admin)
                assert before["operator_bots"] == 2
                assert before["verified"] == 0
                assert before["pending"] == 2
                assert len(before["pending_bots"]) == 2
                first = await _page(admin, 0, 1)
                second = await _page(admin, 1, 1)
                assert first["page"] == 0 and second["page"] == 1
                assert first["pages"] == second["pages"] == 2
                assert len(first["pending_bots"]) == 1
                assert len(second["pending_bots"]) == 1
                assert first["operator_bots"] == 2
                assert first["pending"] == 2

                # CI provisions membership_runtime after migrations.
                # Execute through the real restricted role, not only the
                # privileged migration connection used by other tests.
                await admin.execute(text("SET LOCAL ROLE membership_runtime"))
                try:
                    runtime_page = await _page(admin, 0, 1)
                    assert runtime_page["operator_bots"] == 2
                    assert runtime_page["pending"] == 2
                finally:
                    await admin.execute(text("RESET ROLE"))

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await _page(admin, -1, 8)
                assert getattr(error.value.orig, "sqlstate", None) == "22023"

                # Only the channel with an explicit creator-verification row
                # may count as exempt. Platform operator role is insufficient.
                await admin.execute(text("""
                    INSERT INTO platform_saas_owner_exemptions
                    (id,merchant_id,project_id,bot_instance_id,channel_chat_id,
                     owner_user_id,verified_by,proof_kind)
                    VALUES (:id,:merchant,:project,:bot,:chat,:owner,:owner,
                            'telegram_creator_verified')
                """), {
                    "id": uuid4(), "merchant": merchant,
                    "project": project, "bot": verified_bot,
                    "chat": chat, "owner": owner,
                })
                after = await _report(admin)
                assert after["operator_bots"] == 2
                assert after["verified"] == 1
                assert after["pending"] == 1
                assert len(after["pending_bots"]) == 1
                after_page = await _page(admin, 0, 8)
                assert after_page["operator_bots"] == 2
                assert after_page["verified"] == 1
                assert after_page["pending"] == 1
                assert after_page["pages"] == 1
                assert len(after_page["pending_bots"]) == 1

                # Read-only audit must not switch on billing/holds/trials.
                switches = (await admin.execute(text("""
                    SELECT
                      (SELECT enabled FROM platform_saas_billing_control),
                      (SELECT enabled FROM platform_saas_hold_control),
                      (SELECT enabled FROM platform_saas_access_policy)
                """))).one()
                assert tuple(switches) == (False, False, False)

                await _identity(admin, other_owner)
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await _report(admin)
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
                with pytest.raises(DBAPIError) as denied:
                    async with admin.begin_nested():
                        await _page(admin, 0, 8)
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()
