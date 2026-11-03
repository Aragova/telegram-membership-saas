import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL required for BotInstance tenant RLS integration",
)


async def _make_owner_and_project(sf):
    async with sf() as session:
        async with session.begin():
            user = TelegramUser(
                telegram_user_id=(uuid4().int % 8_000_000_000_000_000_000) + 1,
                username="bot_owner_test",
            )
            session.add(user)
            await session.flush()
            uid = user.id
    async with tenant_session_scope(sf, TenantContext(user_id=uid)) as session:
        project = Project(owner_user_id=uid, name="New bot project")
        session.add(project)
        await session.flush()
        return uid, project.id


async def test_bot_instances_isolated_and_identity_globally_unique():
    engine = build_engine(Settings())
    sf = build_session_factory(engine)
    try:
        owner_a, project_a = await _make_owner_and_project(sf)
        owner_b, project_b = await _make_owner_and_project(sf)
        bot_id = (uuid4().int % 8_000_000_000_000_000_000) + 1

        async with tenant_session_scope(
            sf, TenantContext(user_id=owner_a, project_id=project_a)
        ) as session:
            session.add(
                BotInstance(
                    project_id=project_a,
                    telegram_bot_id=bot_id,
                    username="owned_bot",
                    display_name="Owner bot",
                    token_ciphertext="gAAAAAplaceholder-encrypted-only",
                )
            )
            await session.flush()

        async with tenant_session_scope(
            sf, TenantContext(user_id=owner_b, project_id=project_b)
        ) as session:
            visible = list((await session.scalars(select(BotInstance))).all())
            assert visible == []

        with pytest.raises(IntegrityError):
            async with tenant_session_scope(
                sf, TenantContext(user_id=owner_b, project_id=project_b)
            ) as session:
                session.add(
                    BotInstance(
                        project_id=project_b,
                        telegram_bot_id=bot_id,
                        username="collision",
                        display_name="Stolen",
                        token_ciphertext="gAAAAAplaceholder-encrypted-only",
                    )
                )
                await session.flush()

        async with tenant_session_scope(
            sf, TenantContext(user_id=owner_a, project_id=project_a)
        ) as session:
            registered = list((await session.scalars(select(BotInstance))).all())
            assert len(registered) == 1
            assert registered[0].telegram_bot_id == bot_id
            assert registered[0].status == "registered"
    finally:
        await engine.dispose()
