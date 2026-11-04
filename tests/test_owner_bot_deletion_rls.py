"""Real PostgreSQL: safe detach -> worker drain -> purge -> reconnect loop.

A bot credential is not a payment ledger. Deleting the attachment must not
erase any project, tariff, payment, customer or other tenant's data.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.customer_supervisor import (
    CustomerBotSupervisor,
    discover_removal_targets,
)
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.deletion_service import (
    request_bot_retirement,
)
from telegram_membership_saas.modules.bots.health_service import (
    claim_bot_health,
    write_bot_health,
)
from telegram_membership_saas.modules.bots.runtime_health import (
    CustomerBotRuntimeHealth,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
    owner_home,
)
from telegram_membership_saas.project_worker_lock import (
    ProjectWorkerBusy,
    project_worker_lock,
)
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL required for actual tenant bot retirement test",
)


def _user_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_delete_reconnect_same_bot_twice_and_preserve_project():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    telegram_bot_id = _user_id()
    supervisor = CustomerBotSupervisor(engine, factory, Settings())
    try:
        alice = await ensure_telegram_actor(
            factory, telegram_id=_user_id(),
            username="owner_delete_a", first_name="A",
        )
        bob = await ensure_telegram_actor(
            factory, telegram_id=_user_id(),
            username="owner_delete_b", first_name="B",
        )
        old_project = await create_owner_project(
            factory, actor_id=alice, name="Keep billing history",
        )
        new_project = await create_owner_project(
            factory, actor_id=alice, name="Brand-new onboarding",
        )
        outsider = await create_owner_project(
            factory, actor_id=bob, name="Other owner's project",
        )
        for project_id in (old_project, new_project):
            instance_id = uuid4()
            async with tenant_session_scope(
                factory, TenantContext(user_id=alice, project_id=project_id)
            ) as session:
                session.add(BotInstance(
                    id=instance_id,
                    project_id=project_id,
                    telegram_bot_id=telegram_bot_id,
                    username="reusable_test_bot",
                    display_name="Test",
                    token_ciphertext="gAAAAAplaceholder_secret",
                    status="registered",
                ))
            worker_id = uuid4()
            async with project_worker_lock(engine, project_id) as lock:
                await claim_bot_health(
                    lock, project_id=project_id,
                    bot_id=telegram_bot_id, instance_id=worker_id,
                )
                await write_bot_health(
                    factory, project_id=project_id,
                    bot_id=telegram_bot_id, instance_id=worker_id,
                    state="running",
                )
                with pytest.raises((PermissionError, TenantAccessDenied)):
                    await request_bot_retirement(
                        factory, actor_id=bob, project_id=project_id,
                        bot_instance_id=instance_id,
                    )
                assert await request_bot_retirement(
                    factory, actor_id=alice, project_id=project_id,
                    bot_instance_id=instance_id,
                )
                # The real customer worker retains its lock. Finalization is
                # impossible until it finishes draining and releases it.
                with pytest.raises(ProjectWorkerBusy):
                    async with project_worker_lock(engine, project_id):
                        pytest.fail("retirement must not bypass the live worker")
                assert await request_bot_retirement(
                    factory, actor_id=alice, project_id=project_id,
                    bot_instance_id=instance_id,
                )
                assert (project_id, telegram_bot_id) in await discover_removal_targets(
                    factory
                )
                await supervisor.finalize_removals()
                # Cleanup cannot race a running old poller.
                async with tenant_session_scope(
                    factory, TenantContext(user_id=alice, project_id=project_id)
                ) as session:
                    state = await session.scalar(
                        select(BotInstance).where(BotInstance.id == instance_id)
                    )
                    assert state.status == "deleting"

            # Once the worker releases its lock, supervisor cleans up the
            # token and bot runtime data without any manual SQL operation.
            await supervisor.finalize_removals()
            async with tenant_session_scope(
                factory, TenantContext(user_id=alice, project_id=project_id)
            ) as session:
                assert await session.scalar(
                    select(BotInstance).where(BotInstance.id == instance_id)
                ) is None
                assert await session.scalar(
                    select(CustomerBotRuntimeHealth).where(
                        CustomerBotRuntimeHealth.project_id == project_id
                    )
                ) is None
            # A retired bot can be reused in a DIFFERENT project, because the
            # globally unique Telegram identity and ciphertext were removed.
            assert len((await owner_home(factory, actor_id=alice)).projects) == 2
            assert await request_bot_retirement(
                factory, actor_id=alice, project_id=project_id,
                bot_instance_id=instance_id,
            ) is False
        # No cross-tenant impact, including the unrelated owner's project.
        assert (await owner_home(factory, actor_id=bob)).projects == (
            (outsider, "Other owner's project"),
        )
    finally:
        await engine.dispose()
