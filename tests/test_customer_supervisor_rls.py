"""Real PostgreSQL tests for safe discovery, liveness RLS and project fencing."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.customer_supervisor import discover_targets
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.health_service import write_bot_health
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.project_worker_lock import (
    ProjectWorkerBusy,
    project_worker_lock,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL required for PostgreSQL supervisor integration",
)


async def test_trusted_global_discovery_returns_only_metadata_and_rls_hides_ciphertext():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        telegram_id = (uuid4().int % 8_000_000_000_000_000_000) + 1
        owner = await ensure_telegram_actor(
            factory, telegram_id=telegram_id,
            username="supervisor_owner", first_name="Owner",
        )
        project_id = await create_owner_project(
            factory, actor_id=owner, name="Automatic bot project"
        )
        bot_id = (uuid4().int % 8_000_000_000_000_000_000) + 1
        ciphertext = "gAAAAA-only-test-ciphertext"
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id)
        ) as session:
            session.add(BotInstance(
                project_id=project_id, telegram_bot_id=bot_id,
                username="automatic_buyer_bot", display_name="Buyer",
                token_ciphertext=ciphertext, status="registered",
            ))

        # This call is safe globally because SECURITY DEFINER only returns
        # three metadata columns and is restricted to the trusted runtime role.
        listed = await discover_targets(factory)
        matches = [entry for entry in listed if entry.project_id == project_id]
        assert len(matches) == 1
        assert matches[0].telegram_bot_id == bot_id
        assert ciphertext not in repr(matches[0])

        # RLS still blocks direct ciphertext reads from an unscoped worker.
        async with factory() as session:
            direct = await session.scalar(
                select(BotInstance).where(BotInstance.project_id == project_id)
            )
        assert direct is None

        worker_a, worker_b = uuid4(), uuid4()
        await write_bot_health(
            factory, project_id=project_id, bot_id=bot_id,
            instance_id=worker_a, state="starting",
        )
        await write_bot_health(
            factory, project_id=project_id, bot_id=bot_id,
            instance_id=worker_a, state="running",
        )
        # A newly acquired lock can take over; old shutdown may not overwrite it.
        await write_bot_health(
            factory, project_id=project_id, bot_id=bot_id,
            instance_id=worker_b, state="starting",
        )
        await write_bot_health(
            factory, project_id=project_id, bot_id=bot_id,
            instance_id=worker_a, state="queued",
        )
        await write_bot_health(
            factory, project_id=project_id, bot_id=bot_id,
            instance_id=worker_a, state="stopped",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id)
        ) as session:
            health = await session.get(CustomerBotRuntimeHealth, project_id)
            assert health.state == "starting"
            assert health.instance_id == worker_b

        stranger = await ensure_telegram_actor(
            factory,
            telegram_id=(uuid4().int % 8_000_000_000_000_000_000) + 1,
            username="stranger", first_name="Someone",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=stranger)
        ) as session:
            assert (await session.scalars(
                select(CustomerBotRuntimeHealth)
            )).all() == []
    finally:
        await engine.dispose()


async def test_project_worker_lock_prevents_simultaneous_pollers():
    engine = build_engine(Settings())
    project_id = uuid4()
    try:
        async with project_worker_lock(engine, project_id):
            with pytest.raises(ProjectWorkerBusy):
                async with project_worker_lock(engine, project_id):
                    pytest.fail("a second poller must never obtain the same lease")
        # Physically closing the first lock-holding PostgreSQL session releases it.
        async with project_worker_lock(engine, project_id):
            pass
    finally:
        await engine.dispose()
