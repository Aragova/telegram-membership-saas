from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.access import (
    AccessAction,
    AccessActionStatus,
    AccessActionType,
    AccessCommand,
    AccessRuntimeResult,
    AccessState,
    mark_access_action_failed,
    mark_access_action_succeeded,
    reconcile_access_target,
)
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.payments import get_or_create_project_customer
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.modules.telegram_access.adapter import (
    TelegramAccessCommand,
    TelegramAccessResult,
)
from telegram_membership_saas.modules.telegram_access.models import (
    TelegramJoinTicket,
    TelegramJoinTicketStatus,
)


@dataclass(frozen=True, slots=True)
class OwnerServiceAccessResult:
    grant: EntitlementGrant
    runtime: AccessRuntimeResult


@dataclass(frozen=True, slots=True)
class JoinRequestDecision:
    approve: bool
    ticket_id: UUID | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Telegram access timestamps must be timezone-aware")


def parse_chat_id(external_id: str) -> int | str:
    normalized = external_id.strip()
    if not normalized:
        raise ValueError("Telegram resource external_id must not be empty")
    if normalized.lstrip("-").isdigit():
        return int(normalized)
    return normalized


async def build_telegram_access_command(
    session: AsyncSession,
    action: AccessAction,
) -> TelegramAccessCommand:
    if action.status != AccessActionStatus.PROCESSING.value or action.attempt_count <= 0:
        raise ValueError("Access action must be claimed before Telegram execution")

    telegram_user_id = await session.scalar(
        select(TelegramUser.telegram_user_id)
        .join(ProjectCustomer, ProjectCustomer.user_id == TelegramUser.id)
        .where(
            ProjectCustomer.id == action.customer_id,
            ProjectCustomer.project_id == action.project_id,
        )
    )
    if telegram_user_id is None:
        raise RuntimeError("Telegram customer identity could not be resolved")

    resource = await session.scalar(
        select(Resource).where(
            Resource.id == action.resource_id,
            Resource.project_id == action.project_id,
        )
    )
    if resource is None:
        raise RuntimeError("Telegram resource could not be resolved")
    if resource.status != ResourceStatus.ACTIVE.value:
        raise ValueError("Telegram resource is not active")
    if resource.resource_type not in {
        ResourceType.TELEGRAM_CHANNEL.value,
        ResourceType.TELEGRAM_GROUP.value,
    }:
        raise ValueError("Resource is not managed by the Telegram access adapter")

    return TelegramAccessCommand(
        access=AccessCommand(
            action_id=action.id,
            project_id=action.project_id,
            customer_id=action.customer_id,
            resource_id=action.resource_id,
            action_type=AccessActionType(action.action_type),
            claim_attempt=action.attempt_count,
        ),
        telegram_user_id=telegram_user_id,
        chat_id=parse_chat_id(resource.external_id),
    )


async def record_grant_result(
    session: AsyncSession,
    *,
    command: TelegramAccessCommand,
    result: TelegramAccessResult,
    recorded_at: datetime | None = None,
) -> TelegramJoinTicket | None:
    if result.invite_link is None:
        return None
    if result.invite_expires_at is None:
        raise ValueError("Telegram invite result must include an expiry timestamp")
    if result.resolved_chat_id is None:
        raise ValueError("Telegram invite result must include a canonical numeric chat ID")
    require_aware(result.invite_expires_at)
    moment = recorded_at or utcnow()
    require_aware(moment)

    existing = await session.scalar(
        select(TelegramJoinTicket)
        .where(
            TelegramJoinTicket.project_id == command.access.project_id,
            TelegramJoinTicket.access_action_id == command.access.action_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )

    action = await session.scalar(
        select(AccessAction)
        .where(
            AccessAction.id == command.access.action_id,
            AccessAction.project_id == command.access.project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if action is None:
        raise ValueError("Access action is not accessible in the current project scope")
    if (
        action.status != AccessActionStatus.PROCESSING.value
        or action.attempt_count != command.access.claim_attempt
    ):
        raise ValueError("Access action lease is no longer current")
    if existing is not None:
        if existing.claim_attempt > command.access.claim_attempt:
            raise ValueError("Telegram join ticket belongs to a newer claim generation")
        existing.claim_attempt = command.access.claim_attempt
        existing.telegram_user_id = command.telegram_user_id
        existing.chat_id = str(result.resolved_chat_id)
        existing.invite_link = result.invite_link
        existing.status = TelegramJoinTicketStatus.PENDING.value
        existing.expires_at = result.invite_expires_at
        existing.approved_at = None
        existing.closed_at = None
        await session.flush()
        return existing

    await session.execute(
        update(TelegramJoinTicket)
        .where(
            TelegramJoinTicket.project_id == command.access.project_id,
            TelegramJoinTicket.customer_id == command.access.customer_id,
            TelegramJoinTicket.resource_id == command.access.resource_id,
            TelegramJoinTicket.status == TelegramJoinTicketStatus.PENDING.value,
        )
        .values(
            status=TelegramJoinTicketStatus.REVOKED.value,
            closed_at=moment,
        )
    )

    ticket = TelegramJoinTicket(
        project_id=command.access.project_id,
        access_action_id=command.access.action_id,
        claim_attempt=command.access.claim_attempt,
        customer_id=command.access.customer_id,
        resource_id=command.access.resource_id,
        telegram_user_id=command.telegram_user_id,
        chat_id=str(result.resolved_chat_id),
        invite_link=result.invite_link,
        status=TelegramJoinTicketStatus.PENDING.value,
        expires_at=result.invite_expires_at,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def close_pending_tickets_for_revoke(
    session: AsyncSession,
    *,
    command: TelegramAccessCommand,
    closed_at: datetime | None = None,
) -> None:
    moment = closed_at or utcnow()
    require_aware(moment)
    await session.execute(
        update(TelegramJoinTicket)
        .where(
            TelegramJoinTicket.project_id == command.access.project_id,
            TelegramJoinTicket.customer_id == command.access.customer_id,
            TelegramJoinTicket.resource_id == command.access.resource_id,
            TelegramJoinTicket.status == TelegramJoinTicketStatus.PENDING.value,
        )
        .values(
            status=TelegramJoinTicketStatus.REVOKED.value,
            closed_at=moment,
        )
    )
    await session.flush()


async def close_ticket_for_failed_delivery(
    session: AsyncSession,
    *,
    command: TelegramAccessCommand,
    closed_at: datetime | None = None,
) -> None:
    moment = closed_at or utcnow()
    require_aware(moment)
    await session.execute(
        update(TelegramJoinTicket)
        .where(
            TelegramJoinTicket.project_id == command.access.project_id,
            TelegramJoinTicket.access_action_id == command.access.action_id,
            TelegramJoinTicket.claim_attempt == command.access.claim_attempt,
            TelegramJoinTicket.status == TelegramJoinTicketStatus.PENDING.value,
        )
        .values(
            status=TelegramJoinTicketStatus.REVOKED.value,
            closed_at=moment,
        )
    )
    await session.flush()


async def ensure_owner_service_access(
    session: AsyncSession,
    *,
    project_id: UUID,
    resource_id: UUID,
    granted_at: datetime | None = None,
) -> OwnerServiceAccessResult:
    moment = granted_at or utcnow()
    require_aware(moment)

    project = await session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise ValueError("Project is not accessible in the current tenant scope")

    resource = await session.scalar(
        select(Resource).where(
            Resource.id == resource_id,
            Resource.project_id == project_id,
            Resource.status == ResourceStatus.ACTIVE.value,
        )
    )
    if resource is None:
        raise ValueError("Active resource is not accessible in the current project scope")

    customer = await get_or_create_project_customer(
        session,
        project_id=project_id,
        user_id=project.owner_user_id,
    )

    await session.execute(
        insert(EntitlementGrant)
        .values(
            project_id=project_id,
            customer_id=customer.id,
            resource_id=resource_id,
            source=EntitlementSource.OWNER_SERVICE.value,
            source_id=project_id,
            status=EntitlementGrantStatus.ACTIVE.value,
            valid_from=moment,
            valid_until=None,
            revoked_at=None,
        )
        .on_conflict_do_update(
            index_elements=["project_id", "source", "source_id", "resource_id"],
            set_={
                "customer_id": customer.id,
                "status": EntitlementGrantStatus.ACTIVE.value,
                "valid_until": None,
                "revoked_at": None,
            },
        )
    )

    grant = await session.scalar(
        select(EntitlementGrant).where(
            EntitlementGrant.project_id == project_id,
            EntitlementGrant.resource_id == resource_id,
            EntitlementGrant.source == EntitlementSource.OWNER_SERVICE.value,
            EntitlementGrant.source_id == project_id,
        )
    )
    if grant is None:
        raise RuntimeError("Owner service grant could not be resolved")

    runtime = await reconcile_access_target(
        session,
        project_id=project_id,
        customer_id=customer.id,
        resource_id=resource_id,
        calculated_at=moment,
    )
    return OwnerServiceAccessResult(grant=grant, runtime=runtime)


async def ensure_owner_service_for_project(
    session: AsyncSession,
    *,
    project_id: UUID,
    granted_at: datetime | None = None,
) -> tuple[OwnerServiceAccessResult, ...]:
    resource_ids = tuple(
        (
            await session.scalars(
                select(Resource.id)
                .where(
                    Resource.project_id == project_id,
                    Resource.status == ResourceStatus.ACTIVE.value,
                    Resource.resource_type.in_(
                        (
                            ResourceType.TELEGRAM_CHANNEL.value,
                            ResourceType.TELEGRAM_GROUP.value,
                        )
                    ),
                )
                .order_by(Resource.created_at, Resource.id)
            )
        ).all()
    )
    results = []
    for resource_id in resource_ids:
        results.append(
            await ensure_owner_service_access(
                session,
                project_id=project_id,
                resource_id=resource_id,
                granted_at=granted_at,
            )
        )
    return tuple(results)


async def _load_ticket_state(
    session: AsyncSession,
    ticket: TelegramJoinTicket,
) -> AccessState:
    state = await session.scalar(
        select(AccessState)
        .where(
            AccessState.project_id == ticket.project_id,
            AccessState.customer_id == ticket.customer_id,
            AccessState.resource_id == ticket.resource_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if state is None:
        raise RuntimeError("Access state disappeared for Telegram join ticket")
    return state


async def _expire_ticket(
    session: AsyncSession,
    ticket: TelegramJoinTicket,
    *,
    moment: datetime,
) -> None:
    ticket.status = TelegramJoinTicketStatus.EXPIRED.value
    ticket.closed_at = moment
    try:
        await mark_access_action_failed(
            session,
            project_id=ticket.project_id,
            action_id=ticket.access_action_id,
            claim_attempt=ticket.claim_attempt,
            error="Telegram join invitation expired before approval",
            failed_at=moment,
            retry_at=moment,
            permanent=False,
        )
    except ValueError as exc:
        # A newer worker generation may already own this action. The expired ticket is
        # still closed, but it must not overwrite the newer lease.
        if "lease is no longer current" not in str(exc) and (
            "Only a processing access action can fail" not in str(exc)
        ):
            raise


async def expire_due_join_tickets(
    session: AsyncSession,
    *,
    project_id: UUID,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    moment = now or utcnow()
    require_aware(moment)
    if limit <= 0:
        raise ValueError("limit must be positive")

    tickets = tuple(
        (
            await session.scalars(
                select(TelegramJoinTicket)
                .where(
                    TelegramJoinTicket.project_id == project_id,
                    TelegramJoinTicket.status == TelegramJoinTicketStatus.PENDING.value,
                    TelegramJoinTicket.expires_at <= moment,
                )
                .order_by(TelegramJoinTicket.expires_at, TelegramJoinTicket.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for ticket in tickets:
        await _expire_ticket(session, ticket, moment=moment)
    await session.flush()
    return len(tickets)


async def prepare_join_request_decision(
    session: AsyncSession,
    *,
    project_id: UUID,
    invite_link: str,
    chat_id: int | str,
    telegram_user_id: int,
    requested_at: datetime | None = None,
) -> JoinRequestDecision:
    moment = requested_at or utcnow()
    require_aware(moment)

    ticket = await session.scalar(
        select(TelegramJoinTicket)
        .where(
            TelegramJoinTicket.project_id == project_id,
            TelegramJoinTicket.invite_link == invite_link,
            TelegramJoinTicket.chat_id == str(chat_id),
            TelegramJoinTicket.telegram_user_id == telegram_user_id,
            TelegramJoinTicket.status == TelegramJoinTicketStatus.PENDING.value,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if ticket is None:
        return JoinRequestDecision(approve=False)

    if ticket.expires_at <= moment:
        await _expire_ticket(session, ticket, moment=moment)
        return JoinRequestDecision(approve=False, ticket_id=ticket.id)

    state = await _load_ticket_state(session, ticket)
    if not state.desired_access:
        ticket.status = TelegramJoinTicketStatus.REVOKED.value
        ticket.closed_at = moment
        await session.flush()
        return JoinRequestDecision(approve=False, ticket_id=ticket.id)

    return JoinRequestDecision(approve=True, ticket_id=ticket.id)


async def finalize_join_request_approval(
    session: AsyncSession,
    *,
    project_id: UUID,
    ticket_id: UUID,
    approved_at: datetime | None = None,
) -> TelegramJoinTicket:
    moment = approved_at or utcnow()
    require_aware(moment)

    ticket = await session.scalar(
        select(TelegramJoinTicket)
        .where(
            TelegramJoinTicket.id == ticket_id,
            TelegramJoinTicket.project_id == project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if ticket is None:
        raise ValueError("Telegram join ticket is not accessible in the current project scope")

    ticket.status = TelegramJoinTicketStatus.APPROVED.value
    ticket.approved_at = moment
    ticket.closed_at = moment
    await session.flush()

    await mark_access_action_succeeded(
        session,
        project_id=project_id,
        action_id=ticket.access_action_id,
        claim_attempt=ticket.claim_attempt,
        processed_at=moment,
    )
    await session.flush()
    return ticket
