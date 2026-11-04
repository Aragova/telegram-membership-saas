"""Regression: periodic owner-service reconciliation must respect explicit revocation."""
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
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.owner_setup.obligations import revoke_owner_grant
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.modules.telegram_access.service import (
    ensure_owner_service_access,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS integration",
)


async def test_reconciliation_does_not_resurrect_explicitly_revoked_owner_grant():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        actor_id = await ensure_telegram_actor(
            factory,
            telegram_id=(uuid4().int % 9_000_000_000) + 1_000_000_000,
            username="owner_access_revoke_test",
            first_name="Owner",
        )
        project_id = await create_owner_project(
            factory, actor_id=actor_id, name="Owner service revocation",
        )
        resource_id = uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id),
        ) as session:
            session.add(Resource(
                id=resource_id, project_id=project_id,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=str(-(uuid4().int % 9_000_000_000 + 1_000_000_000)),
                name="Owner-owned test resource",
                status=ResourceStatus.ACTIVE.value,
            ))
            await session.flush()
            first = await ensure_owner_service_access(
                session, project_id=project_id, resource_id=resource_id,
            )
            grant_id = first.grant.id
            assert first.grant.status == EntitlementGrantStatus.ACTIVE.value

        change = await revoke_owner_grant(
            factory, actor_id=actor_id, project_id=project_id, grant_id=grant_id,
        )
        assert change.changed

        # Real runtime calls this periodically while the bot remains registered.
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id),
        ) as session:
            repeat = await ensure_owner_service_access(
                session, project_id=project_id, resource_id=resource_id,
            )
            assert repeat.grant.id == grant_id
            await session.refresh(repeat.grant)
            assert repeat.grant.status == EntitlementGrantStatus.REVOKED.value
            assert repeat.grant.revoked_at is not None
            assert repeat.runtime.effective_access.has_access is False
            grants = (await session.scalars(
                select(EntitlementGrant).where(
                    EntitlementGrant.project_id == project_id,
                    EntitlementGrant.source == EntitlementSource.OWNER_SERVICE.value,
                    EntitlementGrant.resource_id == resource_id,
                )
            )).all()
            assert len(grants) == 1
    finally:
        await engine.dispose()
