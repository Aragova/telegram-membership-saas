import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.access import (
    AccessState,
    EffectiveAccess,
    claim_next_access_action,
    mark_access_action_succeeded,
    recalculate_effective_access,
)
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for access lock-order tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_user(session_factory, username: str) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            user = TelegramUser(telegram_user_id=_random_telegram_id(), username=username)
            session.add(user)
            await session.flush()
            return user.id


async def _create_project(session_factory, owner_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id),
    ) as session:
        project = Project(owner_user_id=owner_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def test_access_success_uses_same_target_lock_order_as_recalculation() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 2, 7, 12, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory, "lock_order_owner")
        buyer_id = await _create_user(session_factory, "lock_order_buyer")
        project_id = await _create_project(session_factory, owner_id, "Lock order")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            customer = await get_or_create_project_customer(
                session,
                project_id=project_id,
                user_id=buyer_id,
            )
            resource = Resource(
                project_id=project_id,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=f"-100-lock-order-{uuid4()}",
                name="Lock order channel",
                status=ResourceStatus.ACTIVE.value,
            )
            session.add(resource)
            await session.flush()

            grant = EntitlementGrant(
                project_id=project_id,
                customer_id=customer.id,
                resource_id=resource.id,
                source=EntitlementSource.MANUAL.value,
                source_id=uuid4(),
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=base - timedelta(minutes=1),
                valid_until=base + timedelta(hours=1),
            )
            session.add(grant)
            await session.flush()

            await recalculate_effective_access(
                session,
                project_id=project_id,
                customer_id=customer.id,
                resource_id=resource.id,
                calculated_at=base,
            )
            action = await claim_next_access_action(
                session,
                project_id=project_id,
                claimed_at=base,
            )
            assert action is not None
            action_id = action.id
            claim_attempt = action.attempt_count
            customer_id = customer.id
            resource_id = resource.id

        callback_started = asyncio.Event()

        async def complete_external_action() -> None:
            async with tenant_session_scope(
                session_factory,
                TenantContext(user_id=owner_id, project_id=project_id),
            ) as callback_session:
                callback_started.set()
                await mark_access_action_succeeded(
                    callback_session,
                    project_id=project_id,
                    action_id=action_id,
                    claim_attempt=claim_attempt,
                    processed_at=base + timedelta(seconds=1),
                )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as lock_session:
            effective = await lock_session.scalar(
                select(EffectiveAccess)
                .where(
                    EffectiveAccess.project_id == project_id,
                    EffectiveAccess.customer_id == customer_id,
                    EffectiveAccess.resource_id == resource_id,
                )
                .with_for_update()
            )
            assert effective is not None

            callback_task = asyncio.create_task(complete_external_action())
            await callback_started.wait()
            await asyncio.sleep(0.1)

            # Recalculation's second target lock must remain obtainable while the
            # callback waits for EffectiveAccess. The old AccessState->EffectiveAccess
            # callback ordering formed a deadlock here.
            state = await lock_session.scalar(
                select(AccessState)
                .where(
                    AccessState.project_id == project_id,
                    AccessState.customer_id == customer_id,
                    AccessState.resource_id == resource_id,
                )
                .with_for_update()
            )
            assert state is not None

        await asyncio.wait_for(callback_task, timeout=3)
    finally:
        await engine.dispose()
