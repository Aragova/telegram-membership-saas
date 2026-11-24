"""PostgreSQL RLS: platform reporting is restricted and audited; tenants isolated."""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules import platform_dashboard_handlers as ui
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.platform_dashboard import platform_report
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL")
    or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires real PostgreSQL runtime RLS and owner migration connection",
)


def _id():
    return (uuid4().int % 7_000_000_000_000_000_000) + 1


async def test_platform_aggregate_requires_db_grant_and_does_not_weaken_rls():
    settings = Settings()
    admin_engine = create_async_engine(settings.migration_database_url)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    operator_tg = _id()
    regular_tg = _id()
    try:
        op = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username="platform_operator",
            first_name="Operator",
        )
        other = await ensure_telegram_actor(
            factory, telegram_id=regular_tg, username="platform_regular",
            first_name="Regular",
        )
        own_project = await create_owner_project(
            factory, actor_id=op, name="Operator's tenant",
        )
        outside_project = await create_owner_project(
            factory, actor_id=other, name="Independent tenant",
        )
        bot_names = []
        for actor_id, project in (
            (op, own_project), (other, outside_project),
        ):
            bot_id = _id()
            bot_name = "platform_test_" + str(bot_id) + "_bot"
            bot_names.append(bot_name)
            async with tenant_session_scope(
                factory, TenantContext(user_id=actor_id, project_id=project),
            ) as session:
                session.add(BotInstance(
                    project_id=project, telegram_bot_id=bot_id,
                    username=bot_name, display_name="Sample",
                    token_ciphertext="test-encrypted-value", status="registered",
                ))
                if actor_id == op:
                    session.add(CustomerBotRuntimeHealth(
                        project_id=project, telegram_bot_id=bot_id,
                        instance_id=uuid4(), state="error",
                        last_heartbeat=datetime(2026, 11, 9, 18, tzinfo=UTC),
                        error_code="missing_admin_rights",
                    ))
                session.add(Resource(
                    project_id=project, resource_type="telegram_channel",
                    external_id=str(-_id()), name="Example channel",
                    status="active",
                ))
                session.add(Plan(
                    project_id=project, name="Premium",
                    duration_value=1, duration_unit="month", status="active",
                ))

        # A normal tenant, even if it owns a project, cannot enumerate others.
        with pytest.raises(DBAPIError):
            await platform_report(
                factory, actor_user_id=other, actor_telegram_id=regular_tg,
                settings=Settings(platform_owner_telegram_id=regular_tg),
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=other, project_id=outside_project),
        ) as session:
            visible = (await session.scalars(select(BotInstance.username))).all()
            assert bot_names[0] not in visible
            assert bot_names[1] in visible

        # Grants are written only through privileged migration DB credentials.
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO platform_operators(telegram_user_id, role) "
                    "VALUES (:id, 'owner')"
                ), {"id": operator_tg},
            )
        config = Settings(platform_owner_telegram_id=operator_tg)
        result = await platform_report(
            factory, actor_user_id=op, actor_telegram_id=operator_tg,
            settings=config,
        )
        assert result.counts["owners"] >= 2
        assert result.counts["bots"] >= 2
        assert result.counts["plans_published"] >= 2
        assert any(
            row["username"] == bot_names[0] for row in result.bots
        )
        assert any(
            row["username"] == bot_names[1] for row in result.bots
        )
        diagnostics = next(
            row for row in result.bots if row["username"] == bot_names[0]
        )
        assert diagnostics["error_code"] == "missing_admin_rights"
        assert diagnostics["last_heartbeat"] is not None
        assert all(
            "last_heartbeat" in row and "error_code" in row for row in result.bots
        )
        assert "token_ciphertext" not in diagnostics
        assert result.turnover == () or all(
            "currency" in row and "amount" in row
            for row in result.turnover
        )

        # REPEATABLE READ freezes the other report rows across both calls.
        # Configure it before tenant_session_scope issues any RLS queries.
        snapshot_factory = build_session_factory(
            engine.execution_options(isolation_level="REPEATABLE READ"),
        )
        async with tenant_session_scope(
            snapshot_factory, TenantContext(user_id=op, project_id=own_project),
        ) as session:
            health = (await session.scalars(select(CustomerBotRuntimeHealth).where(
                CustomerBotRuntimeHealth.project_id == own_project,
            ))).one()
            now = (await session.execute(text("SELECT now()"))).scalar_one()
            health.state = "running"
            health.last_heartbeat = now + timedelta(hours=1)
            await session.flush()
            query = text(
                "SELECT public.platform_report_snapshot(:since, 0, 20) AS snapshot"
            ).columns(snapshot=JSONB)
            params = {"since": datetime(1970, 1, 1, tzinfo=UTC)}
            future = (await session.execute(query, params)).scalar_one()
            future_row = next(
                row for row in future["bot_rows"] if row["username"] == bot_names[0]
            )
            assert ui._bot_list_button_label(future_row).startswith("🟠")
            health.last_heartbeat = now
            await session.flush()
            fresh = (await session.execute(query, params)).scalar_one()
            fresh_row = next(
                row for row in fresh["bot_rows"] if row["username"] == bot_names[0]
            )
            assert fresh["bots_running"] == future["bots_running"] + 1
            assert ui._bot_list_button_label(fresh_row).startswith("🟢")
            health.state = "error"
            health.last_heartbeat = datetime(2026, 11, 9, 18, tzinfo=UTC)

        # A forged report request cannot bypass the app's configured ID.
        with pytest.raises(PermissionError):
            await platform_report(
                factory, actor_user_id=other, actor_telegram_id=regular_tg,
                settings=config,
            )
        async with admin_engine.connect() as connection:
            audit_count = (await connection.execute(text(
                "SELECT count(*) FROM platform_report_audit "
                "WHERE actor_user_id = :id"
            ), {"id": op})).scalar_one()
            assert audit_count >= 1
    finally:
        async with admin_engine.begin() as connection:
            await connection.execute(text(
                "DELETE FROM platform_operators "
                "WHERE telegram_user_id = :id"
            ), {"id": operator_tg})
        await engine.dispose()
        await admin_engine.dispose()
