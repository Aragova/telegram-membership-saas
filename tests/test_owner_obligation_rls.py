"""Real PostgreSQL RLS: owner-only cancellation and immutable action evidence."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.obligation_audit import (
    OwnerObligationAudit,
)
from telegram_membership_saas.modules.owner_setup.obligations import (
    close_owner_payment,
    end_owner_subscription,
    list_owner_obligations,
    retry_owner_access,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="Requires real PostgreSQL non-bypass runtime RLS",
)


async def test_owner_audit_is_visible_only_to_project_owner():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        alice = await ensure_telegram_actor(
            factory, telegram_id=(uuid4().int % 9000000000) + 1000000000,
            username="audit_alice", first_name="A",
        )
        bob = await ensure_telegram_actor(
            factory, telegram_id=(uuid4().int % 9000000000) + 1000000000,
            username="audit_bob", first_name="B",
        )
        project = await create_owner_project(
            factory, actor_id=alice, name="Audit-isolated project",
        )
        other_project = await create_owner_project(
            factory, actor_id=bob, name="Unrelated owner",
        )
        result = await list_owner_obligations(
            factory, actor_id=alice, project_id=project,
        )
        assert not result.blockers.has_blockers
        with pytest.raises(PermissionError):
            await list_owner_obligations(
                factory, actor_id=bob, project_id=project,
            )
        target_id = uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=alice, project_id=project)
        ) as session:
            session.add(OwnerObligationAudit(
                project_id=project, actor_id=alice,
                action="owner_test", target_id=target_id, reason="RLS isolation",
            ))
        async with tenant_session_scope(
            factory, TenantContext(user_id=alice, project_id=project)
        ) as session:
            own = list((await session.scalars(
                select(OwnerObligationAudit).where(
                    OwnerObligationAudit.target_id == target_id
                )
            )).all())
            assert len(own) == 1
        async with tenant_session_scope(
            factory, TenantContext(user_id=bob, project_id=other_project)
        ) as session:
            assert not (await session.scalars(
                select(OwnerObligationAudit).where(
                    OwnerObligationAudit.target_id == target_id
                )
            )).all()
    finally:
        await engine.dispose()


async def test_owner_actions_fail_closed_for_missing_targets_and_cross_tenant():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        alice = await ensure_telegram_actor(
            factory, telegram_id=(uuid4().int % 9000000000) + 1000000000,
            username="target_alice", first_name="A",
        )
        bob = await ensure_telegram_actor(
            factory, telegram_id=(uuid4().int % 9000000000) + 1000000000,
            username="target_bob", first_name="B",
        )
        project = await create_owner_project(
            factory, actor_id=alice, name="Owner target project",
        )
        for action in (close_owner_payment, end_owner_subscription, retry_owner_access):
            target = uuid4()
            key = (
                "request_id" if action is close_owner_payment else
                "subscription_id" if action is end_owner_subscription else
                "state_id"
            )
            with pytest.raises(PermissionError):
                await action(
                    factory, actor_id=bob, project_id=project, **{key: target},
                )
            with pytest.raises(ValueError):
                await action(
                    factory, actor_id=alice, project_id=project, **{key: target},
                )
    finally:
        await engine.dispose()
