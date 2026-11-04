"""Real PostgreSQL: transactional immutable metering and bot-deletion history."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
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
from telegram_membership_saas.modules.platform_usage import platform_usage_report
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL")
    or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and non-bypass runtime role",
)


def _telegram_id() -> int:
    return (uuid4().int % 7_000_000_000_000_000_000) + 1


async def test_usage_tracks_connect_delete_publish_and_rollbacks_without_tenant_read():
    settings = Settings()
    admin_engine = create_async_engine(settings.migration_database_url)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    operator_tg = _telegram_id()
    regular_tg = _telegram_id()
    bot_id = uuid4()
    try:
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username="meter_operator",
            first_name="Operator",
        )
        regular = await ensure_telegram_actor(
            factory, telegram_id=regular_tg, username="meter_regular",
            first_name="Regular",
        )
        own_project = await create_owner_project(
            factory, actor_id=operator, name="Metering owner",
        )
        other_project = await create_owner_project(
            factory, actor_id=regular, name="Independent tenant",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=operator, project_id=own_project),
        ) as session:
            session.add(BotInstance(
                id=bot_id, project_id=own_project,
                telegram_bot_id=_telegram_id(),
                username="usage_meter_demo_bot", display_name="Demo",
                token_ciphertext="encrypted-test-only",
                status="registered",
            ))
            session.add(Plan(
                project_id=own_project, name="Meter plan",
                status="draft", duration_unit="month", duration_value=1,
            ))
        async with tenant_session_scope(
            factory, TenantContext(user_id=operator, project_id=own_project),
        ) as session:
            current = await session.get(BotInstance, bot_id)
            current.display_name = "Renamed"  # no extra connection event
            current.status = "deleting"
        async with tenant_session_scope(
            factory, TenantContext(user_id=operator, project_id=own_project),
        ) as session:
            current = await session.get(BotInstance, bot_id)
            await session.delete(current)

        async with admin_engine.connect() as session:
            history = (await session.execute(
                text(
                    "SELECT event_type, bot_username FROM platform_usage_events "
                    "WHERE source_id = :id ORDER BY id"
                ), {"id": bot_id},
            )).all()
        assert [kind for kind, _username in history] == [
            "bot_connected", "bot_deleting", "bot_removed",
        ]
        assert all(name == "usage_meter_demo_bot" for _, name in history)

        failed_id = uuid4()
        with pytest.raises(RuntimeError, match="cancelled operation"):
            async with tenant_session_scope(
                factory, TenantContext(user_id=operator, project_id=own_project),
            ) as session:
                session.add(BotInstance(
                    id=failed_id, project_id=own_project,
                    telegram_bot_id=_telegram_id(),
                    username="uncommitted_demo_bot", display_name="Uncommitted",
                    token_ciphertext="encrypted-test-only", status="registered",
                ))
                await session.flush()
                raise RuntimeError("cancelled operation")
        async with admin_engine.connect() as session:
            assert (await session.execute(text(
                "SELECT count(*) FROM platform_usage_events "
                "WHERE source_id = :id"
            ), {"id": failed_id})).scalar_one() == 0

        # A standard tenant cannot read or insert global usage event rows.
        async with tenant_session_scope(
            factory, TenantContext(user_id=regular, project_id=other_project),
        ) as session:
            assert (await session.execute(text(
                "SELECT count(*) FROM platform_usage_events"
            ))).scalar_one() == 0
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=regular, project_id=other_project),
            ) as session:
                await session.execute(text(
                    "INSERT INTO platform_usage_events "
                    "(project_id, event_type, source_id) "
                    "VALUES (:p, 'bot_connected', :id)"
                ), {"p": other_project, "id": uuid4()})

        with pytest.raises(DBAPIError):
            await platform_usage_report(
                factory, actor_user_id=regular, actor_telegram_id=regular_tg,
                settings=Settings(platform_owner_telegram_id=regular_tg),
            )

        async with admin_engine.begin() as session:
            await session.execute(text(
                "INSERT INTO platform_operators(telegram_user_id, role) "
                "VALUES (:id, 'owner')"
            ), {"id": operator_tg})
        snapshot = await platform_usage_report(
            factory, actor_user_id=operator, actor_telegram_id=operator_tg,
            period="all",
            settings=Settings(platform_owner_telegram_id=operator_tg),
        )
        assert snapshot.counts["bot_connected"] >= 1
        assert snapshot.counts["bot_removed"] >= 1
        assert snapshot.event_total >= 3
        async with admin_engine.connect() as session:
            audit = (await session.execute(text(
                "SELECT count(*) FROM platform_report_audit "
                "WHERE actor_user_id = :id AND report_kind = 'usage'"
            ), {"id": operator})).scalar_one()
            assert audit >= 1
    finally:
        async with admin_engine.begin() as session:
            await session.execute(text(
                "DELETE FROM platform_operators "
                "WHERE telegram_user_id = :id"
            ), {"id": operator_tg})
        await engine.dispose()
        await admin_engine.dispose()
