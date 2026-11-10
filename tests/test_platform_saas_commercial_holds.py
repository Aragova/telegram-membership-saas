"""PostgreSQL RLS integration: opt-in overdue holds never touch paid rights.

The integration database is throwaway CI PostgreSQL. Feature switches are
restored even when an assertion fails, and no staging customers are touched.
"""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires real PostgreSQL with non-bypass runtime role",
)


async def _switch(admin_engine, *, hold=False, billing=False, cutoff=None, grace=7):
    async with admin_engine.begin() as admin:
        since = cutoff if cutoff is not None else datetime.now(UTC) - timedelta(days=1)
        await admin.execute(text("""
            UPDATE platform_saas_hold_control SET
              enabled=:hold,effective_from=:start,grace_days=:grace
            WHERE id=1
        """), {"hold": hold, "start": since if hold else None, "grace": grace})
        await admin.execute(text("""
            UPDATE platform_saas_billing_control SET
              enabled=:billing,effective_from=:start WHERE id=1
        """), {"billing": billing, "start": since if billing else None})


async def _owner_state(admin_engine, owner, *, at=None):
    async with admin_engine.connect() as admin:
        return (await admin.execute(text(
            "SELECT public.platform_saas_owner_hold_state(:owner,:at)"
        ), {"owner": owner, "at": at or datetime.now(UTC)})).scalar_one()


async def test_off_by_default_grace_and_confirmed_receipt_release_holds():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    operator_tg = _tid()
    try:
        owner, merchant, project, existing_bot, _ = await _project(
            factory, _tid(),
        )
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        other_project = await create_owner_project(
            factory, actor_id=owner, name="Overdue second project",
        )
        now = datetime.now(UTC)
        # Invoice before rollout is not retroactively a hold by itself.
        assert await _owner_state(admin_engine, owner) == "disabled"
        await _switch(admin_engine, billing=True, hold=False)
        assert await _owner_state(admin_engine, owner) == "disabled"

        async with admin_engine.begin() as admin:
            await admin.execute(text("""
                INSERT INTO platform_operators(telegram_user_id,role)
                VALUES (:tg,'owner')
            """), {"tg": operator_tg})
            # The 0043 ledger guard requires an actual bot fee source.
            # The backdated service period is a test fixture, NOT a runtime
            # way to generate invoices or bypass the 0044 invoice issuer.
            invoice = await _invoice(
                admin, owner, merchant, existing_bot,
                period_start=now - timedelta(days=15),
            )
            await _line(
                admin, invoice, project, "bot_fee", existing_bot,
            )
            await admin.execute(text(
                "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
            ), {"id": invoice})
        assert await _owner_state(admin_engine, owner) == "disabled"
        # A seven-day grace starts from the later of due, issuance and
        # rollout. Future-point-in-time checks do not change DB clock.
        await _switch(admin_engine, billing=True, hold=True, grace=7)
        assert await _owner_state(admin_engine, owner) == "grace"
        assert await _owner_state(
            admin_engine, owner, at=now + timedelta(days=5),
        ) == "grace"
        assert await _owner_state(
            admin_engine, owner, at=now + timedelta(days=9),
        ) == "held"

        # In real time, zero-day grace makes this source immediately overdue.
        await _switch(admin_engine, billing=True, hold=True, grace=0)
        assert await _owner_state(admin_engine, owner) == "held"
        async with admin_engine.connect() as admin:
            # Test each entry point at the DB boundary. No additional
            # unrelated checkout columns are required for the P6711 guard
            # to run, because it is a BEFORE INSERT trigger.
            for table in (
                "payment_requests", "file_sale_manual_orders",
                "file_sale_purchases",
            ):
                async with admin.begin():
                    # Real checkouts bind a trusted project scope before
                    # entering the SECURITY DEFINER trigger.
                    await admin.execute(text(
                        "SELECT set_config('app.current_project_id',:id,true)"
                    ), {"id": str(project)})
                    statement = (
                        "INSERT INTO " + table + " (id,project_id) "
                        "VALUES (:id,:project)"
                    )
                    with pytest.raises(DBAPIError) as error:
                        async with admin.begin_nested():
                            await admin.execute(text(statement), {
                                "id": uuid4(), "project": project,
                            })
                    assert getattr(error.value.orig, "sqlstate", None) == "P6711"
            # A forged/unbound project must not disclose another owner's
            # overdue status through a SECURITY DEFINER BEFORE INSERT.
            async with admin.begin():
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "INSERT INTO file_sale_purchases(id,project_id) "
                            "VALUES (:id,:project)"
                        ), {"id": uuid4(), "project": project})
                assert getattr(error.value.orig, "sqlstate", None) != "P6711"

        # Resource creation has its own pre-existing owner/RLS guard.
        # Exercise the actual authenticated owner path, not a raw
        # unscoped admin INSERT that the earlier guard correctly rejects.
        with pytest.raises(DBAPIError) as error:
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                session.add(Resource(
                    project_id=project, resource_type="telegram_channel",
                    external_id="-100" + str(uuid4().int % 10**12).zfill(12),
                    name="New channel during hold", status="active",
                ))
                await session.flush()
        assert getattr(error.value.orig, "sqlstate", None) == "P6711"

        # Same owner cannot evade an overdue invoice via new Merchant ID.
        with pytest.raises(DBAPIError) as error:
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=other_project),
            ) as session:
                session.add(BotInstance(
                    project_id=other_project, telegram_bot_id=_tid(),
                    username="unpaid_second", display_name="Unpaid second",
                    token_ciphertext="test-placeholder", status="registered",
                ))
                await session.flush()
        assert getattr(error.value.orig, "sqlstate", None) == "P6711"

        # The ORIGINAL bot is never disabled; existing access isn't touched.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            existing = await session.get(BotInstance, existing_bot)
            assert existing.status == "registered"
        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                await admin.execute(text(
                    "SELECT set_config('app.current_user_id',:uid,true)"
                ), {"uid": str(operator)})
                report = (await admin.execute(text(
                    "SELECT public.platform_saas_hold_invoice_report(0,6) AS snapshot"
                ).columns(snapshot=JSONB))).scalar_one()
                assert report["held_owners"] == 1
                assert report["holds_enabled"] is True
                receipt = (await admin.execute(text(
                    "SELECT public.platform_saas_confirm_receipt(:id,:ref)"
                ), {"id": invoice, "ref": "bank-reviewed-test"})).scalar_one()
                assert receipt is not None
                await tx.commit()
            except Exception:
                await tx.rollback()
                raise

        assert await _owner_state(admin_engine, owner) == "clear"
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=other_project),
        ) as session:
            bot = BotInstance(
                project_id=other_project, telegram_bot_id=_tid(),
                username="paid_second", display_name="Paid second",
                token_ciphertext="test-placeholder", status="registered",
            )
            session.add(bot)
            await session.flush()
            assert bot.id is not None

    finally:
        await _switch(admin_engine, billing=False, hold=False)
        await engine.dispose()
        await admin_engine.dispose()


async def test_old_issued_invoice_and_other_owner_never_trigger_hold():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    try:
        owner, merchant, project, bot, _ = await _project(factory, _tid())
        other_owner, _, _, _, _ = await _project(factory, _tid())
        now = datetime.now(UTC)
        async with admin_engine.begin() as admin:
            invoice = await _invoice(
                admin, owner, merchant, bot,
                period_start=now - timedelta(days=15),
            )
            await _line(admin, invoice, project, "bot_fee", bot)
            await admin.execute(text(
                "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
            ), {"id": invoice})
        await _switch(
            admin_engine, billing=True, hold=True, grace=0,
            cutoff=now + timedelta(minutes=5),
        )
        # Policy is not active until the future cutoff.
        assert await _owner_state(admin_engine, owner) == "disabled"
        # Even after rollout, a pre-rollout invoice is NOT grandfathered
        # into an automatic hold; a new approved charge is needed.
        assert await _owner_state(
            admin_engine, owner, at=now + timedelta(days=1),
        ) == "clear"
        assert await _owner_state(
            admin_engine, other_owner, at=now + timedelta(days=1),
        ) == "clear"
    finally:
        await _switch(admin_engine, billing=False, hold=False)
        await engine.dispose()
        await admin_engine.dispose()
