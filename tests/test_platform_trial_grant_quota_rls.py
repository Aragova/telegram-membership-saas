"""PostgreSQL integration checks for inactive-by-default trial and shared free-grant limit."""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_owner_subscribers_rls import setup_subscriber, tg_id

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.entitlements import EntitlementGrant, EntitlementSource
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.owner_setup.subscribers import (
    activate_manual_plan,
    grant_manual_access,
    revoke_manual_access,
)
from telegram_membership_saas.modules.plans import DurationUnit, Plan, PlanResource, PlanStatus
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires a migrated PostgreSQL database with privileged test role",
)


async def _set_policy(admin_engine, *, active):
    async with admin_engine.begin() as admin:
        await admin.execute(text(
            "UPDATE platform_saas_access_policy "
            "SET enabled=:active,effective_from=:effective WHERE id=1"
        ), {
            "active": active,
            "effective": datetime.now(UTC) - timedelta(hours=1) if active else None,
        })


async def _connect(factory, actor, project, *, status="registered"):
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor, project_id=project),
    ) as session:
        bot = BotInstance(
            project_id=project, telegram_bot_id=tg_id(),
            username="trial_test_bot", display_name="Trial test bot",
            token_ciphertext="placeholder-do-not-use", status=status,
        )
        session.add(bot)
        await session.flush()
        return bot.id


async def _count_claims(admin_engine, owner):
    async with admin_engine.connect() as admin:
        return (await admin.execute(text("""
            SELECT count(*)
            FROM merchant_platform_trial_claims
            WHERE owner_user_id=:owner AND policy_key='vimi-saas-72h-v1'
              AND scope='owner'
        """), {"owner": owner})).scalar_one()


async def test_trial_is_opt_in_once_per_owner_and_first_bot_only():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner = await ensure_telegram_actor(
        factory, telegram_id=tg_id(), username=None, first_name=None,
    )
    old_owner = await ensure_telegram_actor(
        factory, telegram_id=tg_id(), username=None, first_name=None,
    )
    project_a = await create_owner_project(
        factory, actor_id=owner, name="Trial project A",
    )
    project_b = await create_owner_project(
        factory, actor_id=owner, name="Trial project B",
    )
    grandfathered = await create_owner_project(
        factory, actor_id=old_owner, name="Existing merchant",
    )
    try:
        # With policy OFF, connecting does not claim trials or restrict users.
        await _connect(factory, old_owner, grandfathered)
        assert await _count_claims(admin_engine, old_owner) == 0
        await _set_policy(admin_engine, active=True)
        await _connect(factory, owner, project_a)
        assert await _count_claims(admin_engine, owner) == 1
        # Same account, another project & Merchant ID: no second free bot.
        with pytest.raises(DBAPIError) as error:
            await _connect(factory, owner, project_b)
        assert getattr(error.value.orig, "sqlstate", None) == "P6703"
        assert await _count_claims(admin_engine, owner) == 1
        # A pre-rollout user's first trial must not be backdated or issued.
        await _connect(factory, old_owner, grandfathered, status="disabled")
        assert await _count_claims(admin_engine, old_owner) == 0
    finally:
        await _set_policy(admin_engine, active=False)
        await engine.dispose()
        await admin_engine.dispose()


async def test_combined_free_grant_and_plan_activation_quota_is_per_channel():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner, _, _, project, _, customer_a, channel_a, _ = (
        await setup_subscriber(factory)
    )
    other_users = [
        await ensure_telegram_actor(
            factory, telegram_id=tg_id(), username=None, first_name=None,
        ) for _ in range(3)
    ]
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner, project_id=project),
    ) as session:
        extra_customers = [
            ProjectCustomer(project_id=project, user_id=user_id)
            for user_id in other_users
        ]
        other_channel = Resource(
            project_id=project, resource_type="telegram_channel",
            external_id="-100" + str(uuid4().int % 10**12).zfill(12),
            name="Another Channel", status=ResourceStatus.ACTIVE.value,
        )
        session.add_all([*extra_customers, other_channel])
        await session.flush()
        customer_b, customer_c, customer_d = (x.id for x in extra_customers)
        channel_b = other_channel.id
        plan = Plan(
            project_id=project, name="Two-channel premium",
            duration_value=30, duration_unit=DurationUnit.DAY.value,
            status=PlanStatus.ACTIVE.value,
        )
        session.add(plan)
        await session.flush()
        session.add_all([
            PlanResource(project_id=project, plan_id=plan.id, resource_id=channel_a),
            PlanResource(project_id=project, plan_id=plan.id, resource_id=channel_b),
        ])
        plan_id = plan.id
    try:
        await _set_policy(admin_engine, active=True)
        await _connect(factory, owner, project)
        await grant_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_a, resource_id=channel_a, days=7,
        )
        # The same trial owner uses the same three slots regardless of UI
        # path; a multi-channel plan uses one per individual channel.
        activated = await activate_manual_plan(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_b, plan_id=plan_id,
        )
        assert activated.changed
        third = await grant_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_c, resource_id=channel_a, days=7,
        )
        assert third.changed
        await revoke_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_c, grant_id=third.grant_id,
        )
        with pytest.raises(ValueError, match="Лимит бесплатных активаций"):
            await grant_manual_access(
                factory, actor_id=owner, project_id=project,
                customer_id=customer_d, resource_id=channel_a, days=7,
            )
        # A different channel still has remaining slots.
        result = await grant_manual_access(
            factory, actor_id=owner, project_id=project,
            customer_id=customer_d, resource_id=channel_b, days=7,
        )
        assert result.changed
        async with admin_engine.connect() as admin:
            stats = (await admin.execute(text("""
                SELECT channel_chat_id,count(*) AS used
                FROM platform_saas_complimentary_events
                WHERE owner_user_id=:owner GROUP BY channel_chat_id
            """), {"owner": owner})).all()
            assert sorted(int(row.used) for row in stats) == [2, 3]
            assert (await admin.execute(text("""
                SELECT count(*) FROM platform_saas_complimentary_events
                WHERE owner_user_id=:owner AND kind='plan'
            """), {"owner": owner})).scalar_one() == 2
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert await session.scalar(select(func.count(EntitlementGrant.id)).where(
                EntitlementGrant.project_id == project,
                EntitlementGrant.source == EntitlementSource.MANUAL.value,
            )) == 5
    finally:
        await _set_policy(admin_engine, active=False)
        await engine.dispose()
        await admin_engine.dispose()
