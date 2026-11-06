"""Real PostgreSQL verification for permanent merchant identity groundwork."""

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
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL")
    or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and distinct runtime role",
)


def _tid() -> int:
    return (uuid4().int % 7_000_000_000_000_000_000) + 1


async def _merchant(factory, owner_id, project_id):
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner_id, project_id=project_id),
    ) as session:
        return (await session.scalar(select(Project.merchant_id).where(
            Project.id == project_id
        )))


async def test_merchant_identity_survives_bot_and_preserves_independent_businesses():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username="merchant_a", first_name="A",
        )
        other_owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username="merchant_b", first_name="B",
        )
        project_a = await create_owner_project(
            factory, actor_id=owner, name="First business",
        )
        project_b = await create_owner_project(
            factory, actor_id=owner, name="Second independent business",
        )
        project_c = await create_owner_project(
            factory, actor_id=other_owner, name="Other business",
        )
        a = await _merchant(factory, owner, project_a)
        b = await _merchant(factory, owner, project_b)
        c = await _merchant(factory, other_owner, project_c)
        assert len({a, b, c}) == 3

        # A bot can be removed and reattached without deleting the merchant.
        bot_id = uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_a),
        ) as session:
            session.add(BotInstance(
                id=bot_id, project_id=project_a, telegram_bot_id=_tid(),
                username="merchant_test_bot", display_name="Merchant test",
                token_ciphertext="encrypted-test-only",
            ))
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_a),
        ) as session:
            await session.delete(await session.get(BotInstance, bot_id))
        assert await _merchant(factory, owner, project_a) == a

        # Duplicate Chat IDs across different businesses are observations,
        # not proof of ownership or permission to read/merge finances.
        chat = "-100123456789012"
        resource_ids = (uuid4(), uuid4())
        for project_id, actor_id, resource_id in (
            (project_a, owner, resource_ids[0]),
            (project_c, other_owner, resource_ids[1]),
        ):
            async with tenant_session_scope(
                factory, TenantContext(user_id=actor_id, project_id=project_id),
            ) as session:
                session.add(Resource(
                    id=resource_id, project_id=project_id,
                    resource_type="telegram_channel",
                    external_id=chat, name="Shared observed chat",
                ))

        async with admin_engine.connect() as admin:
            rows = (await admin.execute(text(
                "SELECT merchant_id, verified_at FROM merchant_channel_periods "
                "WHERE telegram_chat_id = :chat AND resource_id IN (:a, :b)"
            ), {"chat": chat, "a": resource_ids[0], "b": resource_ids[1]})).all()
            assert len(rows) == 2
            assert {mid for mid, _ in rows} == {a, c}
            assert all(verified_at is None for _, verified_at in rows)
            links = (await admin.execute(text(
                "SELECT merchant_id, project_id FROM merchant_project_periods "
                "WHERE project_id IN (:a, :b, :c) AND ended_at IS NULL"
            ), {"a": project_a, "b": project_b, "c": project_c})).all()
            assert set(links) == {(a, project_a), (b, project_b), (c, project_c)}

        # Even a real owner cannot directly access the private linkage tables.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_a),
        ) as session:
            for table in (
                "merchant_identities", "merchant_project_periods",
                "merchant_channel_periods",
            ):
                assert (await session.execute(text(
                    f"SELECT count(*) FROM {table}"
                ))).scalar_one() == 0

        # Direct switching of merchant or owner cannot steal old history.
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project_a),
            ) as session:
                await session.execute(text(
                    "UPDATE projects SET merchant_id = :other "
                    "WHERE id = :project"
                ), {"other": c, "project": project_a})
        assert await _merchant(factory, owner, project_a) == a

        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project_a),
            ) as session:
                await session.execute(text(
                    "UPDATE projects SET owner_user_id = :other "
                    "WHERE id = :project"
                ), {"other": other_owner, "project": project_a})

        # Deleting a resource ends the observation, never moves it.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_a),
        ) as session:
            await session.delete(await session.get(Resource, resource_ids[0]))
        async with admin_engine.connect() as admin:
            row = (await admin.execute(text(
                "SELECT merchant_id, ended_at FROM merchant_channel_periods "
                "WHERE resource_id = :id"
            ), {"id": resource_ids[0]})).one()
            assert row[0] == a and row[1] is not None
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_failed_project_creation_rolls_back_merchant_and_period():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    try:
        actor = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username="merchant_rollback",
            first_name="Rollback",
        )
        transient_project = uuid4()
        merchant_id = None
        with pytest.raises(RuntimeError, match="registration cancelled"):
            async with tenant_session_scope(
                factory, TenantContext(user_id=actor),
            ) as session:
                project = Project(
                    id=transient_project, owner_user_id=actor,
                    name="Rollback business",
                )
                session.add(project)
                await session.flush()
                merchant_id = project.merchant_id
                raise RuntimeError("registration cancelled")
        assert merchant_id is not None
        async with admin_engine.connect() as admin:
            assert (await admin.execute(text(
                "SELECT count(*) FROM merchant_identities WHERE id = :id"
            ), {"id": merchant_id})).scalar_one() == 0
            assert (await admin.execute(text(
                "SELECT count(*) FROM merchant_project_periods "
                "WHERE project_id = :id"
            ), {"id": transient_project})).scalar_one() == 0
    finally:
        await engine.dispose()
        await admin_engine.dispose()
