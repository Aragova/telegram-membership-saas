import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.access import EffectiveAccess, process_access_recalculate_event
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.outbox import OutboxEvent, OutboxEventStatus, OutboxEventType
from telegram_membership_saas.modules.owner_service import (
    create_owner_service_account,
    reconcile_owner_service_account_for_project,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for effective access integration tests",
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
    async with tenant_session_scope(session_factory, TenantContext(user_id=owner_id)) as session:
        project = Project(owner_user_id=owner_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def _add_resource(session, project_id: UUID, name: str) -> UUID:
    resource = Resource(
        project_id=project_id,
        resource_type=ResourceType.TELEGRAM_CHANNEL.value,
        external_id=str(_random_telegram_id()),
        name=name,
        status=ResourceStatus.ACTIVE.value,
    )
    session.add(resource)
    await session.flush()
    return resource.id


async def _queue_recalculate(session, project_id: UUID, customer_id: UUID, resource_id: UUID) -> UUID:
    event = OutboxEvent(
        project_id=project_id,
        event_type=OutboxEventType.ACCESS_RECALCULATE.value,
        aggregate_type="test",
        aggregate_id=uuid4(),
        dedupe_key=f"test-recalc:{uuid4()}",
        payload={"customer_id": str(customer_id), "resource_id": str(resource_id)},
        status=OutboxEventStatus.PENDING.value,
    )
    session.add(event)
    await session.flush()
    return event.id


async def test_owner_service_and_effective_access_are_idempotent_and_additive() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    now = datetime.now(UTC)

    try:
        owner_id = await _create_user(session_factory, "service_owner")
        service_user_id = await _create_user(session_factory, "service_account")
        project_id = await _create_project(session_factory, owner_id, "Owner Service Project")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id),
        ) as session:
            assignment = await create_owner_service_account(
                session,
                owner_user_id=owner_id,
                service_user_id=service_user_id,
            )
            assignment_id = assignment.id

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            first_resource_id = await _add_resource(session, project_id, "First")
            grants = await reconcile_owner_service_account_for_project(
                session,
                assignment_id=assignment_id,
                project_id=project_id,
                now=now,
            )
            assert len(grants) == 1
            assert grants[0].source == EntitlementSource.OWNER_SERVICE.value
            assert grants[0].valid_until is None
            customer_id = grants[0].customer_id

            recalc = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.project_id == project_id,
                    OutboxEvent.event_type == OutboxEventType.ACCESS_RECALCULATE.value,
                )
            )
            assert recalc is not None
            projection = await process_access_recalculate_event(
                session,
                project_id=project_id,
                event_id=recalc.id,
                as_of=now,
            )
            assert projection.desired_access is True
            sync_event = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.project_id == project_id,
                    OutboxEvent.event_type == OutboxEventType.ACCESS_SYNC.value,
                )
            )
            assert sync_event is not None
            assert sync_event.payload["desired_access"] == "true"

            duplicate = await reconcile_owner_service_account_for_project(
                session,
                assignment_id=assignment_id,
                project_id=project_id,
                now=now,
            )
            assert len(duplicate) == 1
            grant_count = len(
                list(
                    (
                        await session.scalars(
                            select(EntitlementGrant).where(
                                EntitlementGrant.project_id == project_id,
                                EntitlementGrant.source == EntitlementSource.OWNER_SERVICE.value,
                            )
                        )
                    ).all()
                )
            )
            assert grant_count == 1

            second_resource_id = await _add_resource(session, project_id, "Future Resource")
            expanded = await reconcile_owner_service_account_for_project(
                session,
                assignment_id=assignment_id,
                project_id=project_id,
                now=now,
            )
            assert {grant.resource_id for grant in expanded} == {
                first_resource_id,
                second_resource_id,
            }

            owner_grant = await session.scalar(
                select(EntitlementGrant).where(
                    EntitlementGrant.project_id == project_id,
                    EntitlementGrant.resource_id == first_resource_id,
                    EntitlementGrant.source == EntitlementSource.OWNER_SERVICE.value,
                )
            )
            assert owner_grant is not None
            manual_grant = EntitlementGrant(
                project_id=project_id,
                customer_id=customer_id,
                resource_id=first_resource_id,
                source=EntitlementSource.MANUAL.value,
                source_id=uuid4(),
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=now,
                valid_until=None,
            )
            session.add(manual_grant)
            owner_grant.status = EntitlementGrantStatus.REVOKED.value
            owner_grant.revoked_at = now
            await session.flush()

            event_id = await _queue_recalculate(
                session, project_id, customer_id, first_resource_id
            )
            still_allowed = await process_access_recalculate_event(
                session, project_id=project_id, event_id=event_id, as_of=now
            )
            assert still_allowed.desired_access is True

            manual_grant.status = EntitlementGrantStatus.REVOKED.value
            manual_grant.revoked_at = now
            await session.flush()
            event_id = await _queue_recalculate(
                session, project_id, customer_id, first_resource_id
            )
            denied = await process_access_recalculate_event(
                session, project_id=project_id, event_id=event_id, as_of=now
            )
            assert denied.desired_access is False

        other_owner_id = await _create_user(session_factory, "other_owner")
        other_project_id = await _create_project(session_factory, other_owner_id, "Other Project")
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=other_owner_id, project_id=other_project_id),
        ) as session:
            assert list((await session.scalars(select(EffectiveAccess))).all()) == []
            with pytest.raises(ValueError, match="not active or accessible"):
                await reconcile_owner_service_account_for_project(
                    session,
                    assignment_id=assignment_id,
                    project_id=other_project_id,
                    now=now,
                )
    finally:
        await engine.dispose()
