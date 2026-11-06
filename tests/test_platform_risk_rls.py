"""Real PostgreSQL permissions and historical channel-risk reporting."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine, build_session_factory, tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project, ensure_telegram_actor,
)
from telegram_membership_saas.modules.platform_risk import (
    platform_merchant_risk_report,
)
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires real migrated PostgreSQL and restricted runtime role",
)


def _telegram_id():
    return uuid4().int % 7_000_000_000_000_000_000 + 1


async def test_platform_risk_report_is_audited_owner_only_and_never_blocks_transfer():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    first_tg, second_tg = _telegram_id(), _telegram_id()
    chat_id = "-100" + str(uuid4().int % 10**12).zfill(12)
    target_project = None
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=first_tg,
            username=None, first_name=None,
        )
        receiver = await ensure_telegram_actor(
            factory, telegram_id=second_tg,
            username=None, first_name=None,
        )
        old = await create_owner_project(
            factory, actor_id=owner, name="Original transfer owner",
        )
        target_project = await create_owner_project(
            factory, actor_id=receiver, name="New & <owner>\nconnection",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=old),
        ) as session:
            session.add(Resource(
                project_id=old, resource_type="telegram_channel",
                external_id=chat_id, name="Original channel",
            ))
        async with tenant_session_scope(
            factory, TenantContext(user_id=receiver, project_id=target_project),
        ) as session:
            session.add(Resource(
                project_id=target_project, resource_type="telegram_channel",
                external_id=chat_id, name="Moved & <private> channel",
            ))

        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "INSERT INTO platform_operators(telegram_user_id,role) "
                "VALUES (:owner,'owner')"
            ), {"owner": first_tg})
            observed = (await admin.execute(text(
                "SELECT count(*) FROM merchant_risk_observations "
                "WHERE project_id=:project AND telegram_chat_id=:chat "
                "AND risk_kind='historical_chat_other_owner'"
            ), {"project": target_project, "chat": chat_id})).scalar_one()
            assert observed == 1

        # Operator can page this fixed report, but not inspect raw private
        # merchant tables using the normal tenant PostgreSQL connection.
        config = Settings(platform_owner_telegram_id=first_tg)
        overview = await platform_merchant_risk_report(
            factory, actor_user_id=owner, actor_telegram_id=first_tg,
            settings=config, period="all",
        )
        assert overview.total >= 1
        assert overview.pages >= 1
        items = list(overview.observations)
        for page in range(1, overview.pages):
            page_report = await platform_merchant_risk_report(
                factory, actor_user_id=owner,
                actor_telegram_id=first_tg,
                settings=config, period="all", page=page,
            )
            items.extend(page_report.observations)
        found = [r for r in items if r["chat_id"] == chat_id]
        assert len(found) == 1
        assert found[0]["project_name"] == "New & <owner>\nconnection"
        assert found[0]["channel_name"] == "Moved & <private> channel"
        assert found[0]["kind"] == "historical_chat_other_owner"

        with pytest.raises(DBAPIError):
            await platform_merchant_risk_report(
                factory, actor_user_id=receiver, actor_telegram_id=second_tg,
                settings=Settings(platform_owner_telegram_id=second_tg),
                period="all",
            )
        with pytest.raises(PermissionError):
            await platform_merchant_risk_report(
                factory, actor_user_id=receiver, actor_telegram_id=second_tg,
                settings=config, period="all",
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=receiver, project_id=target_project),
        ) as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(
                    "SELECT * FROM merchant_risk_observations"
                ))

        async with admin_engine.connect() as admin:
            audits = (await admin.execute(text(
                "SELECT count(*) FROM platform_report_audit "
                "WHERE actor_user_id=:owner "
                "AND report_kind='merchant_risk_review'"
            ), {"owner": owner})).scalar_one()
            assert audits >= 1
    finally:
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "DELETE FROM platform_operators WHERE telegram_user_id=:owner"
            ), {"owner": first_tg})
            if target_project is not None:
                await admin.execute(text(
                    "DELETE FROM merchant_risk_observations "
                    "WHERE project_id=:project AND telegram_chat_id=:chat"
                ), {"project": target_project, "chat": chat_id})
        await engine.dispose()
        await admin_engine.dispose()
