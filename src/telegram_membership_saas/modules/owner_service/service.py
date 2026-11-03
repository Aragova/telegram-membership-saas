from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.outbox import OutboxEvent, OutboxEventStatus, OutboxEventType
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.owner_service.models import (
    OwnerServiceAccount,
    OwnerServiceStatus,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_owner_service_account(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    service_user_id: UUID,
) -> OwnerServiceAccount:
    assignment = OwnerServiceAccount(
        owner_user_id=owner_user_id,
        service_user_id=service_user_id,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def _get_or_create_customer(
    session: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
) -> ProjectCustomer:
    customer_id = await session.scalar(
        insert(ProjectCustomer)
        .values(project_id=project_id, user_id=user_id)
        .on_conflict_do_nothing(index_elements=["project_id", "user_id"])
        .returning(ProjectCustomer.id)
    )
    if customer_id is None:
        customer_id = await session.scalar(
            select(ProjectCustomer.id).where(
                ProjectCustomer.project_id == project_id,
                ProjectCustomer.user_id == user_id,
            )
        )
    if customer_id is None:
        raise RuntimeError("Project customer could not be resolved")

    customer = await session.scalar(select(ProjectCustomer).where(ProjectCustomer.id == customer_id))
    if customer is None:
        raise RuntimeError("Project customer disappeared after creation")
    return customer


async def reconcile_owner_service_account_for_project(
    session: AsyncSession,
    *,
    assignment_id: UUID,
    project_id: UUID,
    now: datetime | None = None,
) -> list[EntitlementGrant]:
    assignment = await session.scalar(
        select(OwnerServiceAccount).where(
            OwnerServiceAccount.id == assignment_id,
            OwnerServiceAccount.status == OwnerServiceStatus.ACTIVE.value,
        )
    )
    if assignment is None:
        raise ValueError("Owner service account is not active or accessible")

    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.owner_user_id == assignment.owner_user_id,
        )
    )
    if project is None:
        raise PermissionError("Owner service account cannot target this project")

    customer = await _get_or_create_customer(
        session,
        project_id=project_id,
        user_id=assignment.service_user_id,
    )
    resource_ids = list(
        (
            await session.scalars(
                select(Resource.id).where(
                    Resource.project_id == project_id,
                    Resource.status == ResourceStatus.ACTIVE.value,
                )
            )
        ).all()
    )
    moment = now or _utcnow()
    grants: list[EntitlementGrant] = []
    for resource_id in resource_ids:
        grant = await session.scalar(
            select(EntitlementGrant).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source == EntitlementSource.OWNER_SERVICE.value,
                EntitlementGrant.source_id == assignment.id,
                EntitlementGrant.resource_id == resource_id,
            )
        )
        created = False
        if grant is None:
            grant = EntitlementGrant(
                project_id=project_id,
                customer_id=customer.id,
                resource_id=resource_id,
                source=EntitlementSource.OWNER_SERVICE.value,
                source_id=assignment.id,
                status=EntitlementGrantStatus.ACTIVE.value,
                valid_from=moment,
                valid_until=None,
            )
            session.add(grant)
            await session.flush()
            created = True

        if created:
            session.add(
                OutboxEvent(
                    project_id=project_id,
                    event_type=OutboxEventType.ACCESS_RECALCULATE.value,
                    aggregate_type="entitlement_grant",
                    aggregate_id=grant.id,
                    dedupe_key=f"owner-service-grant:{grant.id}",
                    payload={
                        "customer_id": str(customer.id),
                        "resource_id": str(resource_id),
                    },
                    status=OutboxEventStatus.PENDING.value,
                )
            )
        grants.append(grant)

    await session.flush()
    return grants
