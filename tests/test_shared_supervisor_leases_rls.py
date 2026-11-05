"""Real PostgreSQL regression tests for shared supervisor advisory leases.

Verify 100 independent project locks on ONE backend connection, fail-closed
cross-worker locking, and per-statement tenant RLS scoping when concurrent
bot health claims interleave on the shared connection.
"""
import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_lease_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.health_service import claim_bot_health
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.project_worker_lock import (
    ProjectWorkerBusy,
    SharedProjectLeaseManager,
    confirm_project_lock,
    project_worker_lock,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="Real PostgreSQL required for shared lease integration",
)


async def test_100_project_locks_share_one_database_backend_and_exclude_legacy():
    settings = Settings()
    engine = build_lease_engine(settings)
    legacy_engine = build_lease_engine(settings)
    manager = SharedProjectLeaseManager(engine)
    await manager.start()
    projects = [uuid4() for _ in range(100)]
    from contextlib import AsyncExitStack

    try:
        async with AsyncExitStack() as stack:
            handles = [
                await stack.enter_async_context(project_worker_lock(manager, pid))
                for pid in projects
            ]
            pids = await asyncio.gather(*(
                handle.scalar(text("SELECT pg_backend_pid()"))
                for handle in handles
            ))
            assert len(set(pids)) == 1
            # Repeated health fencing never increases the advisory lock count.
            for _ in range(3):
                await asyncio.gather(*(
                    confirm_project_lock(handle, pid)
                    for handle, pid in zip(handles, projects, strict=True)
                ))
            with pytest.raises(ProjectWorkerBusy):
                async with project_worker_lock(legacy_engine, projects[0]):
                    pytest.fail("Legacy polling must never own a shared-lease project")
            with pytest.raises(ProjectWorkerBusy):
                async with project_worker_lock(manager, projects[0]):
                    pytest.fail("Duplicate project locks must be rejected")
            assert await handles[0].scalar(
                text("SELECT current_setting('app.current_project_id')")
            ) == str(projects[0])
            assert await handles[1].scalar(
                text("SELECT current_setting('app.current_project_id')")
            ) == str(projects[1])
        # Closing all 100 project scopes releases each lease.
        async with project_worker_lock(legacy_engine, projects[0]):
            pass
    finally:
        await manager.close()
        await engine.dispose()
        await legacy_engine.dispose()


async def test_interleaved_shared_lease_claims_preserve_project_rls():
    settings = Settings()
    engine = build_engine(settings)
    lease_engine = build_lease_engine(settings)
    factory = build_session_factory(engine)
    manager = SharedProjectLeaseManager(lease_engine)
    await manager.start()
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=(uuid4().int % 8_000_000_000_000_000_000) + 1,
            username="shared_lease_owner", first_name="Owner",
        )
        project_ids = [
            await create_owner_project(
                factory, actor_id=owner, name=f"Shared lease {index}",
            )
            for index in (1, 2)
        ]
        bots = [(uuid4().int % 8_000_000_000_000_000_000) + 1 for _ in project_ids]
        for project_id, bot_id in zip(project_ids, bots, strict=True):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project_id),
            ) as session:
                session.add(BotInstance(
                    project_id=project_id,
                    telegram_bot_id=bot_id,
                    username="test_shared_lease_bot",
                    display_name="Shared lease",
                    token_ciphertext="test-only-not-a-real-token",
                    status="registered",
                ))
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            handles = [
                await stack.enter_async_context(project_worker_lock(manager, project_id))
                for project_id in project_ids
            ]
            instance_ids = [uuid4(), uuid4()]
            # These claims execute concurrently and both use session-level
            # set_config() on the same backend. The handle must reapply the
            # exact project RLS scope for every statement.
            await asyncio.gather(*(
                claim_bot_health(
                    handle,
                    project_id=project_id,
                    bot_id=bot_id,
                    instance_id=instance,
                )
                for handle, project_id, bot_id, instance in zip(
                    handles, project_ids, bots, instance_ids, strict=True,
                )
            ))
            for project_id, bot_id, instance in zip(
                project_ids, bots, instance_ids, strict=True,
            ):
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project_id),
                ) as session:
                    row = await session.get(CustomerBotRuntimeHealth, project_id)
                    assert row is not None
                    assert row.telegram_bot_id == bot_id
                    assert row.instance_id == instance
                    assert row.state == "starting"
    finally:
        await manager.close()
        await lease_engine.dispose()
        await engine.dispose()


async def test_cancelled_one_bot_query_does_not_release_other_project_leases():
    settings = Settings()
    engine = build_lease_engine(settings)
    competing = build_lease_engine(settings)
    manager = SharedProjectLeaseManager(engine)
    await manager.start()
    first, second = uuid4(), uuid4()
    from contextlib import AsyncExitStack

    try:
        async with AsyncExitStack() as stack:
            handle_a = await stack.enter_async_context(project_worker_lock(manager, first))
            handle_b = await stack.enter_async_context(project_worker_lock(manager, second))
            # Cancel a statement while PostgreSQL is actively processing it.
            # No cancellation may reach the shared DB connection.
            query = asyncio.create_task(
                handle_a.execute(text("SELECT pg_sleep(0.15)")),
            )
            await asyncio.sleep(0.04)
            assert not query.done()
            query.cancel()
            with pytest.raises(asyncio.CancelledError):
                await query
            assert not manager.failed
            await confirm_project_lock(handle_b, second)
            with pytest.raises(ProjectWorkerBusy):
                async with project_worker_lock(competing, second):
                    pytest.fail("Cancel of first bot cannot unlock second bot")
            with pytest.raises(ProjectWorkerBusy):
                async with project_worker_lock(competing, first):
                    pytest.fail("Cancelled query cannot drop first bot's lock")
    finally:
        await manager.close()
        await engine.dispose()
        await competing.dispose()


async def test_cancelled_lease_acquisition_releases_unclaimed_project_lock():
    settings = Settings()
    engine = build_lease_engine(settings)
    competing = build_lease_engine(settings)
    manager = SharedProjectLeaseManager(engine)
    await manager.start()
    project_id = uuid4()

    async def interrupted_owner():
        async with project_worker_lock(manager, project_id):
            pytest.fail("Cancelled before project scope must not start poller")

    try:
        # Stall acquisition on the mutex. Cancellation must not interrupt the
        # SQL, and its newly acquired lease must still be cleaned up.
        async with manager._mutex:
            owner = asyncio.create_task(interrupted_owner())
            await asyncio.sleep(0)
            owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert not manager.failed
        async with project_worker_lock(competing, project_id):
            pass
    finally:
        await manager.close()
        await engine.dispose()
        await competing.dispose()
