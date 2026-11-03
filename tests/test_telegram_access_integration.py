import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.access import (
    AccessAction,
    AccessActionStatus,
    AccessActionType,
    AccessState,
    ActualAccessState,
    reconcile_access_target,
)
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.telegram_access import (
    TelegramAccessResult,
    TelegramJoinTicket,
    TelegramJoinTicketStatus,
    ensure_owner_service_access,
    ensure_owner_service_for_project,
    expire_due_join_tickets,
    finalize_join_request_approval,
    prepare_join_request_decision,
    process_chat_join_request,
    record_grant_result,
    run_one_telegram_access_action,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for Telegram access integration tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_user(session_factory, username: str) -> tuple[UUID, int]:
    telegram_user_id = _random_telegram_id()
    async with session_factory() as session:
        async with session.begin():
            user = TelegramUser(
                telegram_user_id=telegram_user_id,
                username=username,
            )
            session.add(user)
            await session.flush()
            return user.id, telegram_user_id


async def _create_project(session_factory, owner_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id),
    ) as session:
        project = Project(owner_user_id=owner_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def _create_resource(
    session_factory,
    *,
    owner_id: UUID,
    project_id: UUID,
    name: str,
) -> UUID:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id, project_id=project_id),
    ) as session:
        resource = Resource(
            project_id=project_id,
            resource_type=ResourceType.TELEGRAM_CHANNEL.value,
            external_id=f"-100{uuid4().int % 10_000_000_000}",
            name=name,
        )
        session.add(resource)
        await session.flush()
        return resource.id


class FakeTelegramAdapter:
    def __init__(self, *, base: datetime) -> None:
        self.invite_ttl = timedelta(minutes=15)
        self.base = base
        self.calls = []
        self.counter = 0

    async def prepare(self, command):
        self.calls.append(("prepare", command))
        if command.access.action_type == AccessActionType.REVOKE:
            return TelegramAccessResult()
        self.counter += 1
        return TelegramAccessResult(
            invite_link=f"https://t.me/+ticket-{self.counter}",
            invite_expires_at=self.base + timedelta(minutes=15 * self.counter),
            resolved_chat_id=int(command.chat_id),
        )

    async def deliver(self, command, result):
        self.calls.append(("deliver", command, result))

    async def discard(self, result):
        self.calls.append(("discard", result))


class FakeJoinBot:
    def __init__(self) -> None:
        self.approved: list[tuple[int, int]] = []
        self.declined: list[tuple[int, int]] = []
        self.revoked: list[tuple[int, str]] = []
        self.notifications: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str, parse_mode: str) -> None:
        self.notifications.append((chat_id, text))

    async def approve_chat_join_request(self, *, chat_id: int, user_id: int) -> None:
        self.approved.append((chat_id, user_id))

    async def decline_chat_join_request(self, *, chat_id: int, user_id: int) -> None:
        self.declined.append((chat_id, user_id))

    async def revoke_chat_invite_link(self, *, chat_id: int, invite_link: str) -> None:
        self.revoked.append((chat_id, invite_link))


def _join_request(
    *,
    project_id: UUID,
    invite_link: str,
    chat_id: int,
    telegram_user_id: int,
):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=telegram_user_id),
        invite_link=SimpleNamespace(
            name=project_id.hex,
            invite_link=invite_link,
        ),
    )


async def test_owner_service_worker_join_request_and_rls() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 14, 0, tzinfo=UTC)

    try:
        owner_id, owner_telegram_id = await _create_user(
            session_factory, f"owner_{uuid4().hex[:8]}"
        )
        project_a = await _create_project(session_factory, owner_id, "Telegram A")
        project_b = await _create_project(session_factory, owner_id, "Telegram B")
        resource_id = await _create_resource(
            session_factory,
            owner_id=owner_id,
            project_id=project_a,
            name="Private channel",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_a),
        ) as session:
            first = await ensure_owner_service_access(
                session,
                project_id=project_a,
                resource_id=resource_id,
                granted_at=base,
            )
            second = await ensure_owner_service_access(
                session,
                project_id=project_a,
                resource_id=resource_id,
                granted_at=base,
            )
            project_results = await ensure_owner_service_for_project(
                session,
                project_id=project_a,
                granted_at=base,
            )
            assert len(project_results) == 1
            assert project_results[0].grant.id == first.grant.id
            assert second.grant.id == first.grant.id
            assert first.grant.source == EntitlementSource.OWNER_SERVICE.value
            assert first.grant.valid_until is None
            assert first.runtime.queued_action is not None
            assert first.runtime.queued_action.action_type == AccessActionType.GRANT.value

        adapter = FakeTelegramAdapter(base=base)
        execution = await run_one_telegram_access_action(
            session_factory,
            project_id=project_a,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=base,
            clock=lambda: base,
        )
        assert execution is not None
        assert execution.status == "awaiting_join"
        assert execution.ticket_id is not None

        async with system_project_session_scope(session_factory, project_a) as session:
            ticket = await session.get(TelegramJoinTicket, execution.ticket_id)
            assert ticket is not None
            assert ticket.status == TelegramJoinTicketStatus.PENDING.value
            assert ticket.telegram_user_id == owner_telegram_id
            assert ticket.claim_attempt == 1

            action = await session.get(AccessAction, execution.action_id)
            assert action is not None
            assert action.status == AccessActionStatus.PROCESSING.value

            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_a,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.desired_access is True
            assert state.actual_state == ActualAccessState.UNKNOWN.value

            invite_link = ticket.invite_link
            chat_id = int(ticket.chat_id)

        async with system_project_session_scope(session_factory, project_b) as session:
            assert list((await session.scalars(select(TelegramJoinTicket))).all()) == []

        bot = FakeJoinBot()
        wrong_request = _join_request(
            project_id=project_a,
            invite_link=invite_link,
            chat_id=chat_id,
            telegram_user_id=owner_telegram_id + 1,
        )
        assert (
            await process_chat_join_request(
                session_factory,
                bot,  # type: ignore[arg-type]
                wrong_request,  # type: ignore[arg-type]
                requested_at=base + timedelta(minutes=1),
            )
            is False
        )
        assert bot.declined == [(chat_id, owner_telegram_id + 1)]

        correct_request = _join_request(
            project_id=project_a,
            invite_link=invite_link,
            chat_id=chat_id,
            telegram_user_id=owner_telegram_id,
        )
        assert (
            await process_chat_join_request(
                session_factory,
                bot,  # type: ignore[arg-type]
                correct_request,  # type: ignore[arg-type]
                requested_at=base + timedelta(minutes=2),
            )
            is True
        )
        assert bot.approved == [(chat_id, owner_telegram_id)]
        assert bot.notifications and bot.notifications[0][0] == owner_telegram_id
        assert "Вы в канале!" in bot.notifications[0][1]

        async with system_project_session_scope(session_factory, project_a) as session:
            ticket = await session.get(TelegramJoinTicket, execution.ticket_id)
            assert ticket is not None
            assert ticket.status == TelegramJoinTicketStatus.APPROVED.value

            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_a,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.actual_state == ActualAccessState.GRANTED.value

            action = await session.get(AccessAction, execution.action_id)
            assert action is not None
            assert action.status == AccessActionStatus.SUCCEEDED.value
    finally:
        await engine.dispose()


async def test_expired_join_ticket_retries_same_access_action() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)

    try:
        owner_id, _ = await _create_user(session_factory, f"expiry_{uuid4().hex[:8]}")
        project_id = await _create_project(session_factory, owner_id, "Expiry")
        resource_id = await _create_resource(
            session_factory,
            owner_id=owner_id,
            project_id=project_id,
            name="Expiry channel",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            await ensure_owner_service_access(
                session,
                project_id=project_id,
                resource_id=resource_id,
                granted_at=base,
            )

        adapter = FakeTelegramAdapter(base=base)
        first = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=base,
            clock=lambda: base,
        )
        assert first is not None
        assert first.status == "awaiting_join"
        assert first.ticket_id is not None

        expired_at = base + timedelta(minutes=16)
        async with system_project_session_scope(session_factory, project_id) as session:
            assert (
                await expire_due_join_tickets(
                    session,
                    project_id=project_id,
                    now=expired_at,
                )
                == 1
            )
            action = await session.get(AccessAction, first.action_id)
            assert action is not None
            assert action.status == AccessActionStatus.FAILED_RETRYABLE.value

        adapter.base = expired_at
        second = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=expired_at,
            clock=lambda: expired_at,
        )
        assert second is not None
        assert second.action_id == first.action_id
        assert second.ticket_id == first.ticket_id
        assert second.status == "awaiting_join"

        async with system_project_session_scope(session_factory, project_id) as session:
            ticket = await session.get(TelegramJoinTicket, second.ticket_id)
            assert ticket is not None
            assert ticket.status == TelegramJoinTicketStatus.PENDING.value
            assert ticket.claim_attempt == 2
            assert ticket.invite_link == "https://t.me/+ticket-2"

            action = await session.get(AccessAction, second.action_id)
            assert action is not None
            assert action.status == AccessActionStatus.PROCESSING.value
            assert action.attempt_count == 2
    finally:
        await engine.dispose()


async def test_stale_grant_result_cannot_replace_newer_ticket_generation() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 15, 30, tzinfo=UTC)

    try:
        owner_id, _ = await _create_user(session_factory, f"stale_{uuid4().hex[:8]}")
        project_id = await _create_project(session_factory, owner_id, "Stale result")
        resource_id = await _create_resource(
            session_factory,
            owner_id=owner_id,
            project_id=project_id,
            name="Stale channel",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            await ensure_owner_service_access(
                session,
                project_id=project_id,
                resource_id=resource_id,
                granted_at=base,
            )

        adapter = FakeTelegramAdapter(base=base)
        first = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=base,
            clock=lambda: base,
        )
        assert first is not None
        assert first.ticket_id is not None

        expired_at = base + timedelta(minutes=16)
        async with system_project_session_scope(session_factory, project_id) as session:
            await expire_due_join_tickets(
                session,
                project_id=project_id,
                now=expired_at,
            )

        adapter.base = expired_at
        second = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=expired_at,
            clock=lambda: expired_at,
        )
        assert second is not None
        assert second.ticket_id == first.ticket_id

        old_command = adapter.calls[0][1]
        old_result = TelegramAccessResult(
            invite_link="https://t.me/+late-old",
            invite_expires_at=expired_at + timedelta(minutes=15),
            resolved_chat_id=int(old_command.chat_id),
        )
        async with system_project_session_scope(session_factory, project_id) as session:
            with pytest.raises(ValueError, match="lease is no longer current"):
                await record_grant_result(
                    session,
                    command=old_command,
                    result=old_result,
                    recorded_at=expired_at,
                )

            ticket = await session.get(TelegramJoinTicket, second.ticket_id)
            assert ticket is not None
            assert ticket.claim_attempt == 2
            assert ticket.invite_link == "https://t.me/+ticket-2"
    finally:
        await engine.dispose()


async def test_join_approval_race_queues_corrective_revoke() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)

    try:
        owner_id, owner_telegram_id = await _create_user(
            session_factory, f"race_{uuid4().hex[:8]}"
        )
        project_id = await _create_project(session_factory, owner_id, "Approval race")
        resource_id = await _create_resource(
            session_factory,
            owner_id=owner_id,
            project_id=project_id,
            name="Race channel",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            owner_access = await ensure_owner_service_access(
                session,
                project_id=project_id,
                resource_id=resource_id,
                granted_at=base,
            )
            grant_id = owner_access.grant.id

        adapter = FakeTelegramAdapter(base=base)
        execution = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=base,
            clock=lambda: base,
        )
        assert execution is not None
        assert execution.ticket_id is not None

        async with system_project_session_scope(session_factory, project_id) as session:
            ticket = await session.get(TelegramJoinTicket, execution.ticket_id)
            assert ticket is not None
            decision = await prepare_join_request_decision(
                session,
                project_id=project_id,
                invite_link=ticket.invite_link,
                chat_id=int(ticket.chat_id),
                telegram_user_id=owner_telegram_id,
                requested_at=base + timedelta(minutes=1),
            )
            assert decision.approve is True

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            grant = await session.get(EntitlementGrant, grant_id)
            assert grant is not None
            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = base + timedelta(minutes=2)
            await session.flush()
            flipped = await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=grant.customer_id,
                resource_id=resource_id,
                calculated_at=base + timedelta(minutes=2),
            )
            assert flipped.access_state.desired_access is False

        async with system_project_session_scope(session_factory, project_id) as session:
            await finalize_join_request_approval(
                session,
                project_id=project_id,
                ticket_id=execution.ticket_id,
                approved_at=base + timedelta(minutes=3),
            )
            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.desired_access is False
            assert state.actual_state == ActualAccessState.GRANTED.value

            revoke = await session.scalar(
                select(AccessAction)
                .where(
                    AccessAction.project_id == project_id,
                    AccessAction.resource_id == resource_id,
                    AccessAction.action_type == AccessActionType.REVOKE.value,
                    AccessAction.status.in_(
                        (
                            AccessActionStatus.PENDING.value,
                            AccessActionStatus.FAILED_RETRYABLE.value,
                        )
                    ),
                )
                .order_by(AccessAction.created_at.desc())
            )
            assert revoke is not None

        adapter.base = base + timedelta(minutes=3)
        revoked = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=base + timedelta(minutes=3),
            clock=lambda: base + timedelta(minutes=3),
        )
        assert revoked is not None

        async with system_project_session_scope(session_factory, project_id) as session:
            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.actual_state == ActualAccessState.REVOKED.value
    finally:
        await engine.dispose()
