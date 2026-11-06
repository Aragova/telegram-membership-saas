"""Real PostgreSQL tests for cross-project channel reuse continuity guard."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine, build_session_factory, tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project, ensure_telegram_actor,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and distinct runtime role",
)


def _telegram_id() -> int:
    return uuid4().int % 7_000_000_000_000_000_000 + 1


def _chat_id() -> str:
    return "-100" + str(uuid4().int % 10**12).zfill(12)


async def _resource(factory, actor_id, project_id, chat_id, *, status="active"):
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        resource = Resource(
            project_id=project_id, resource_type="telegram_channel",
            external_id=chat_id, name="Verified Telegram channel",
            status=status,
        )
        session.add(resource)
        await session.flush()
        return resource.id


async def test_same_owner_cannot_reset_channel_history_via_new_project():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_telegram_id(), username=None, first_name=None,
        )
        other = await ensure_telegram_actor(
            factory, telegram_id=_telegram_id(), username=None, first_name=None,
        )
        original = await create_owner_project(factory, actor_id=owner, name="Original")
        replacement = await create_owner_project(factory, actor_id=owner, name="Replacement")
        transferred = await create_owner_project(factory, actor_id=other, name="New owner")
        chat = _chat_id()
        resource_id = await _resource(factory, owner, original, chat)

        # Same owner + same channel is one existing commercial connection.
        with pytest.raises(DBAPIError) as error:
            await _resource(factory, owner, replacement, chat)
        assert getattr(error.value.orig, "sqlstate", None) == "P6501"

        # An old project may retain financial history even when the channel
        # association was removed. A past observation still protects it.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=original),
        ) as session:
            await session.delete(await session.get(Resource, resource_id))
        with pytest.raises(DBAPIError) as error:
            await _resource(factory, owner, replacement, chat)
        assert getattr(error.value.orig, "sqlstate", None) == "P6501"

        # Another owner is not silently tied to the previous owner's
        # merchant identity, nor told anything about their financial history.
        await _resource(factory, other, transferred, chat)
        async with tenant_session_scope(
            factory, TenantContext(user_id=other, project_id=transferred),
        ) as session:
            their_merchant = await session.scalar(select(Project.merchant_id).where(
                Project.id == transferred
            ))
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=original),
        ) as session:
            original_merchant = await session.scalar(select(Project.merchant_id).where(
                Project.id == original
            ))
        assert original_merchant != their_merchant

        # Removing a history-free project clears the blocker, rather than
        # turning its orphaned observation into a permanent account lock.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=original),
        ) as session:
            await session.delete(await session.get(Project, original))
        await _resource(factory, owner, replacement, chat)
    finally:
        await engine.dispose()


async def test_concurrent_same_owner_channel_claims_only_one_wins():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_telegram_id(), username=None, first_name=None,
        )
        left = await create_owner_project(factory, actor_id=owner, name="Left")
        right = await create_owner_project(factory, actor_id=owner, name="Right")
        chat = _chat_id()

        results = await asyncio.gather(
            _resource(factory, owner, left, chat),
            _resource(factory, owner, right, chat),
            return_exceptions=True,
        )
        assert sum(isinstance(result, DBAPIError) for result in results) == 1
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        failure = next(result for result in results if isinstance(result, DBAPIError))
        assert getattr(failure.orig, "sqlstate", None) == "P6501"

        # Exactly one current observation is committed.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=left),
        ) as session:
            count = await session.scalar(text(
                "SELECT count(*) FROM merchant_channel_periods"
            ))
            assert count == 0  # Permanent merchant history never gets tenant SELECT access.
    finally:
        await engine.dispose()
