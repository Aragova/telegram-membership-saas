"""PostgreSQL RLS: platform reporting is restricted and audited; tenants isolated."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

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
        assert result.turnover == () or all(
            "currency" in row and "amount" in row
            for row in result.turnover
        )

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
