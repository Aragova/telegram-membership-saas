"""Real PostgreSQL regression: keep the project owner's channel membership.

After a bot fails to ban the channel creator, historical actual_state='granted'
must not prevent credential rotation/deletion once the owner's entitlements
are revoked. Buyer memberships must still stop deletion.
"""
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.access import AccessState, EffectiveAccess
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.deletion_service import (
    count_retirement_blockers,
    request_bot_retirement,
)
from telegram_membership_saas.modules.owner_setup.obligations import list_owner_obligations
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="Requires real PostgreSQL with tenant RLS",
)


def _telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_only_owner_orphaned_membership_is_exempt_from_bot_retirement():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner_id = await ensure_telegram_actor(
            factory, telegram_id=_telegram_id(),
            username="owner_retirement", first_name="Owner",
        )
        buyer_id = await ensure_telegram_actor(
            factory, telegram_id=_telegram_id(),
            username="buyer_retirement", first_name="Buyer",
        )
        project_id = await create_owner_project(
            factory, actor_id=owner_id, name="Owner channel membership",
        )
        resource_id, instance_id = uuid4(), uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner_id, project_id=project_id)
        ) as session:
            session.add(Resource(
                id=resource_id, project_id=project_id,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=str(-_telegram_id()),
                name="Owner channel", status=ResourceStatus.ACTIVE.value,
            ))
            session.add(BotInstance(
                id=instance_id, project_id=project_id,
                telegram_bot_id=_telegram_id(),
                username="owner_channel_bot", display_name="Owner channel bot",
                token_ciphertext="test-encrypted-placeholder", status="registered",
            ))
            await session.flush()
            owner_customer = await get_or_create_project_customer(
                session, project_id=project_id, user_id=owner_id,
            )
            owner_customer_id = owner_customer.id
            now = datetime.now(UTC)
            session.add(EffectiveAccess(
                project_id=project_id, customer_id=owner_customer_id,
                resource_id=resource_id, has_access=False, active_grant_count=0,
                revision=2, computed_at=now,
            ))
            await session.flush()
            session.add(AccessState(
                project_id=project_id, customer_id=owner_customer_id,
                resource_id=resource_id, desired_access=False,
                desired_revision=2, actual_state="granted",
                last_error="Telegram cannot remove channel creator",
            ))
        snapshot = await list_owner_obligations(
            factory, actor_id=owner_id, project_id=project_id,
        )
        assert snapshot.blockers.access == 0
        assert snapshot.access == ()
        # The recorded Telegram membership is NOT falsified or deleted.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner_id, project_id=project_id)
        ) as session:
            state = await session.scalar(select(AccessState).where(
                AccessState.customer_id == owner_customer_id,
                AccessState.resource_id == resource_id,
            ))
            assert state.actual_state == "granted"
            assert state.last_error is not None
            buyer_customer = await get_or_create_project_customer(
                session, project_id=project_id, user_id=buyer_id,
            )
            buyer_customer_id = buyer_customer.id
            session.add(EffectiveAccess(
                project_id=project_id, customer_id=buyer_customer_id,
                resource_id=resource_id, has_access=False, active_grant_count=0,
                revision=2, computed_at=datetime.now(UTC),
            ))
            await session.flush()
            session.add(AccessState(
                project_id=project_id, customer_id=buyer_customer_id,
                resource_id=resource_id, desired_access=False,
                desired_revision=2, actual_state="granted",
            ))

        # Never exempt a buyer who also has no active entitlements.
        snapshot = await list_owner_obligations(
            factory, actor_id=owner_id, project_id=project_id,
        )
        assert snapshot.blockers.access == 1
        assert len(snapshot.access) == 1
        assert snapshot.access[0].customer_id == buyer_customer_id
        with pytest.raises(ValueError, match="неотозванных доступов"):
            await request_bot_retirement(
                factory, actor_id=owner_id, project_id=project_id,
                bot_instance_id=instance_id,
            )

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner_id, project_id=project_id)
        ) as session:
            buyer_state = await session.scalar(select(AccessState).where(
                AccessState.customer_id == buyer_customer_id,
                AccessState.resource_id == resource_id,
            ))
            buyer_state.actual_state = "revoked"

        snapshot = await list_owner_obligations(
            factory, actor_id=owner_id, project_id=project_id,
        )
        assert not snapshot.blockers.has_blockers
        assert await request_bot_retirement(
            factory, actor_id=owner_id, project_id=project_id,
            bot_instance_id=instance_id,
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner_id, project_id=project_id)
        ) as session:
            instance = await session.scalar(select(BotInstance).where(
                BotInstance.id == instance_id,
            ))
            assert instance.status == "deleting"
            state = await session.scalar(select(AccessState).where(
                AccessState.customer_id == owner_customer_id,
            ))
            assert state.actual_state == "granted"
    finally:
        await engine.dispose()
