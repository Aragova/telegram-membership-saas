"""Regression: an unbilled new bot must not trap owner onboarding at channel setup.

The live database can hold buyer commerce immediately after the shared owner
trial ends, while the periodic issuer has not yet created the first invoice.
Central billing truthfully shows zero invoices in this state. Free setup must
remain possible without unfreezing buyers or real overdue network holds.
"""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from test_platform_saas_commercial_holds import _switch
from test_platform_saas_invoice_rls import _tid

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
from telegram_membership_saas.modules.projects.models import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires real PostgreSQL with non-bypass runtime role",
)


async def _connect_bot(factory, *, owner, project, label):
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner, project_id=project),
    ) as session:
        bot = BotInstance(
            project_id=project, telegram_bot_id=_tid(),
            username=label, display_name=label,
            token_ciphertext="dummy-fixture-only", status="registered",
        )
        session.add(bot)
        await session.flush()
        return bot.id


async def test_channel_setup_allowed_without_invoice_while_buyer_hold_remains():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    now = datetime.now(UTC)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username=None, first_name="Owner",
        )
        prior_project = await create_owner_project(
            factory, actor_id=owner, name="Existing network bot",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=prior_project),
        ) as session:
            merchant = await session.scalar(
                select(Project.merchant_id).where(Project.id == prior_project),
            )
        # Seed an already-finished original account trial, BEFORE connecting
        # the test bots. This does not edit a real immutable trial claim.
        async with admin_engine.begin() as admin:
            await admin.execute(text("""
                INSERT INTO public.merchant_platform_trial_claims
                  (id,merchant_id,owner_user_id,policy_key,scope,claimed_at)
                VALUES
                  (:id,:merchant,:owner,'vimi-saas-72h-v1','owner',:claimed)
            """), {
                "id": uuid4(), "merchant": merchant, "owner": owner,
                "claimed": now - timedelta(days=8),
            })
        await _connect_bot(
            factory, owner=owner, project=prior_project, label="existing_vimi_bot",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="New bot on existing account",
        )
        bot = await _connect_bot(
            factory, owner=owner, project=project, label="new_vimi_bot",
        )

        await _switch(
            admin_engine, billing=True, hold=True,
            cutoff=now - timedelta(days=2), grace=0,
        )
        async with admin_engine.connect() as admin:
            account_state = (await admin.execute(text(
                "SELECT public.platform_saas_owner_overdue_state(:owner,:at)"
            ), {"owner": owner, "at": datetime.now(UTC)})).scalar_one()
            bot_state = (await admin.execute(text(
                "SELECT public.platform_saas_bot_hold_state(:bot,:at)"
            ), {"bot": bot, "at": datetime.now(UTC)})).scalar_one()
            issued = (await admin.execute(text("""
                SELECT count(*) FROM public.platform_saas_invoices
                WHERE owner_user_id=:owner AND status='issued'
            """), {"owner": owner})).scalar_one()
        assert issued == 0
        assert account_state == "clear"
        assert bot_state == "held"

        # This used to fail with P6711 and a fake "overdue bill" message.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            channel = Resource(
                project_id=project, resource_type="telegram_channel",
                external_id="-100" + str(uuid4().int % 10**12).zfill(12),
                name="New channel after existing network trial", status="active",
            )
            session.add(channel)
            await session.flush()
            assert channel.id is not None

        # A successful free setup must NOT bypass the paid checkout gate.
        async with admin_engine.connect() as admin:
            async with admin.begin():
                await admin.execute(text(
                    "SELECT set_config('app.current_project_id',:project,true)"
                ), {"project": str(project)})
                with pytest.raises(DBAPIError) as denied:
                    async with admin.begin_nested():
                        await admin.execute(text("""
                            INSERT INTO public.payment_requests(id,project_id)
                            VALUES (:id,:project)
                        """), {"id": uuid4(), "project": project})
                assert getattr(denied.value.orig, "sqlstate", None) == "P6711"
    finally:
        await _switch(admin_engine, billing=False, hold=False)
        await engine.dispose()
        await admin_engine.dispose()
