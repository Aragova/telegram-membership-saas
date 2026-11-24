import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from aiogram.types import Chat, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, User
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
from telegram_membership_saas.modules.entitlements import EntitlementGrant, EntitlementGrantStatus
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    claim_next_outbox_event,
    mark_outbox_event_failed,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.telegram_access import (
    TelegramAccessResult,
    ensure_owner_service_access,
    run_one_telegram_access_action,
)
from telegram_membership_saas.modules.telegram_access.handlers import process_native_departure
from telegram_membership_saas.modules.telegram_access.native_departures import (
    process_claimed_native_departure,
)
from telegram_membership_saas.modules.telegram_access.service import (
    NativeObservationStale,
    reconcile_native_departure,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for Telegram access integration tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_user(session_factory) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            user = TelegramUser(
                telegram_user_id=_random_telegram_id(),
                username=f"late_revoke_{uuid4().hex[:8]}",
            )
            session.add(user)
            await session.flush()
            return user.id


async def _create_project_and_resource(session_factory, owner_id: UUID) -> tuple[UUID, UUID]:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id),
    ) as session:
        project = Project(owner_user_id=owner_id, name="Late revoke")
        session.add(project)
        await session.flush()
        project_id = project.id

    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_id, project_id=project_id),
    ) as session:
        resource = Resource(
            project_id=project_id,
            resource_type=ResourceType.TELEGRAM_CHANNEL.value,
            external_id=f"-100{uuid4().int % 10_000_000_000}",
            name="Late revoke channel",
        )
        session.add(resource)
        await session.flush()
        return project_id, resource.id


class AlreadyMemberThenRevokeAdapter:
    invite_ttl = timedelta(minutes=15)

    def __init__(self) -> None:
        self.revoke_calls = 0
        self.discard_calls = 0

    async def prepare(self, command):
        if command.access.action_type == AccessActionType.REVOKE:
            self.revoke_calls += 1
            return TelegramAccessResult()
        return TelegramAccessResult(
            resolved_chat_id=int(command.chat_id),
            already_member=True,
        )

    async def deliver(self, command, result):
        return None

    async def discard(self, result):
        self.discard_calls += 1


async def test_late_revoke_records_external_effect_after_lease_expiry() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    base = datetime(2026, 11, 3, 17, 0, tzinfo=UTC)

    try:
        owner_id = await _create_user(session_factory)
        project_id, resource_id = await _create_project_and_resource(session_factory, owner_id)

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

        adapter = AlreadyMemberThenRevokeAdapter()
        granted = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=base,
            clock=lambda: base,
        )
        assert granted is not None
        assert granted.status == AccessActionStatus.SUCCEEDED.value

        revoke_at = base + timedelta(minutes=1)
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            grant = await session.get(EntitlementGrant, grant_id)
            assert grant is not None
            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = revoke_at
            await session.flush()
            runtime = await reconcile_access_target(
                session,
                project_id=project_id,
                customer_id=grant.customer_id,
                resource_id=resource_id,
                calculated_at=revoke_at,
            )
            assert runtime.access_state.desired_access is False
            assert runtime.queued_action is not None
            assert runtime.queued_action.action_type == AccessActionType.REVOKE.value

        late_completion = revoke_at + timedelta(seconds=2)
        revoked = await run_one_telegram_access_action(
            session_factory,
            project_id=project_id,
            adapter=adapter,  # type: ignore[arg-type]
            claimed_at=revoke_at,
            lease_timeout=timedelta(seconds=1),
            clock=lambda: late_completion,
        )
        assert revoked is not None
        assert revoked.status == AccessActionStatus.SUCCEEDED.value
        assert adapter.revoke_calls == 1
        assert adapter.discard_calls == 0

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            state = await session.scalar(
                select(AccessState).where(
                    AccessState.project_id == project_id,
                    AccessState.resource_id == resource_id,
                )
            )
            assert state is not None
            assert state.desired_access is False
            assert state.actual_state == ActualAccessState.REVOKED.value
    finally:
        await engine.dispose()


async def test_native_revoke_wait_persists_and_rechecks_after_restart() -> None:
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    base = datetime.now(UTC) - timedelta(minutes=3)
    try:
        owner_id = await _create_user(factory)
        project_id, resource_id = await _create_project_and_resource(factory, owner_id)
        context = TenantContext(user_id=owner_id, project_id=project_id)
        async with tenant_session_scope(factory, context) as session:
            grant = (await ensure_owner_service_access(
                session, project_id=project_id, resource_id=resource_id, granted_at=base,
            )).grant
            grant_id, customer_id = grant.id, grant.customer_id
        adapter = AlreadyMemberThenRevokeAdapter()
        await run_one_telegram_access_action(
            factory, project_id=project_id, adapter=adapter, claimed_at=base, clock=lambda: base,
        )
        moment = base + timedelta(minutes=1)
        async with tenant_session_scope(factory, context) as session:
            grant = await session.get(EntitlementGrant, grant_id)
            grant.status = EntitlementGrantStatus.REVOKED.value
            grant.revoked_at = moment
            await session.flush()
            runtime = await reconcile_access_target(
                session, project_id=project_id, customer_id=customer_id,
                resource_id=resource_id, calculated_at=moment,
            )
            action_id = runtime.queued_action.id

        class ProtectedAdapter(AlreadyMemberThenRevokeAdapter):
            async def prepare(self, command):
                assert command.is_channel
                return TelegramAccessResult(revoke_retry_at=moment + timedelta(minutes=1))

        deferred = await run_one_telegram_access_action(
            factory, project_id=project_id, adapter=ProtectedAdapter(),
            claimed_at=moment, clock=lambda: moment,
        )
        assert deferred.status == "native_subscription_protected"
        async with tenant_session_scope(factory, context) as session:
            action = await session.get(AccessAction, action_id)
            assert action.status == AccessActionStatus.PENDING.value
            assert action.last_error is None and action.locked_at is None
            state = await session.scalar(select(AccessState).where(
                AccessState.project_id == project_id, AccessState.resource_id == resource_id,
            ))
            assert state.actual_state == ActualAccessState.GRANTED.value
        assert await run_one_telegram_access_action(
            factory, project_id=project_id, adapter=AlreadyMemberThenRevokeAdapter(),
            claimed_at=moment, clock=lambda: moment,
        ) is None
        retry = moment + timedelta(minutes=1)
        completed = await run_one_telegram_access_action(
            factory, project_id=project_id, adapter=AlreadyMemberThenRevokeAdapter(),
            claimed_at=retry, clock=lambda: retry,
        )
        assert completed.status == AccessActionStatus.SUCCEEDED.value
    finally:
        await engine.dispose()


async def test_native_departure_requeues_only_existing_local_access() -> None:
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    base = datetime.now(UTC) - timedelta(minutes=1)
    try:
        owner_id = await _create_user(factory)
        project_id, resource_id = await _create_project_and_resource(factory, owner_id)
        context = TenantContext(user_id=owner_id, project_id=project_id)
        async with tenant_session_scope(factory, context) as session:
            await ensure_owner_service_access(
                session, project_id=project_id, resource_id=resource_id, granted_at=base,
            )
            resource = await session.get(Resource, resource_id)
            user = await session.get(TelegramUser, owner_id)
            chat_id, telegram_id = int(resource.external_id), user.telegram_user_id
        await run_one_telegram_access_action(
            factory, project_id=project_id, adapter=AlreadyMemberThenRevokeAdapter(),
            claimed_at=base, clock=lambda: base,
        )
        async with tenant_session_scope(factory, context) as session:
            assert await reconcile_native_departure(
                session, project_id=project_id, chat_id=chat_id,
                telegram_user_id=telegram_id, observed_at=datetime.now(UTC),
            )
            assert not await reconcile_native_departure(
                session, project_id=project_id, chat_id=chat_id,
                telegram_user_id=telegram_id, observed_at=datetime.now(UTC),
            )
            state = await session.scalar(select(AccessState).where(
                AccessState.project_id == project_id, AccessState.resource_id == resource_id,
            ))
            assert state.desired_access
            assert state.actual_state == ActualAccessState.REVOKED.value
            actions = (await session.scalars(select(AccessAction).where(
                AccessAction.project_id == project_id,
                AccessAction.status == AccessActionStatus.PENDING.value,
            ))).all()
            assert len(actions) == 1
            assert actions[0].action_type == AccessActionType.GRANT.value
    finally:
        await engine.dispose()


@pytest.mark.parametrize("current_status", ["left", "member"])
async def test_native_departure_durable_retry_and_live_membership(current_status):
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    base = datetime.now(UTC) - timedelta(minutes=2)
    try:
        owner_id = await _create_user(factory)
        project_id, resource_id = await _create_project_and_resource(factory, owner_id)
        context = TenantContext(user_id=owner_id, project_id=project_id)
        async with tenant_session_scope(factory, context) as session:
            await ensure_owner_service_access(
                session, project_id=project_id, resource_id=resource_id, granted_at=base,
            )
            resource = await session.get(Resource, resource_id)
            person = await session.get(TelegramUser, owner_id)
            chat_id, telegram_id = int(resource.external_id), person.telegram_user_id
        await run_one_telegram_access_action(
            factory, project_id=project_id, adapter=AlreadyMemberThenRevokeAdapter(),
            claimed_at=base, clock=lambda: base,
        )
        # The fixture's database IDs can exceed Telegram's documented ID range;
        # aiogram accepts the integer and no live request is made.
        user = User(id=telegram_id, is_bot=False, first_name="Test")
        update = ChatMemberUpdated(
            chat=Chat(id=chat_id, type="channel"), from_user=user,
            date=base + timedelta(minutes=1),
            old_chat_member=ChatMemberMember(user=user, until_date=base),
            new_chat_member=ChatMemberLeft(user=user),
        )
        assert await process_native_departure(
            factory, update, project_id=project_id, update_id=123,
        )
        assert not await process_native_departure(
            factory, update, project_id=project_id, update_id=123,
        )
        async with system_project_session_scope(factory, project_id) as session:
            event = await claim_next_outbox_event(session, project_id=project_id)
            event_id, attempt = event.id, event.attempt_count
        bot = AsyncMock()
        bot.get_chat_member.side_effect = RuntimeError("temporary network failure")
        with pytest.raises(RuntimeError, match="temporary network"):
            await process_claimed_native_departure(
                factory, project_id=project_id, event_id=event_id,
                claim_attempt=attempt, bot=bot,
            )
        async with system_project_session_scope(factory, project_id) as session:
            await mark_outbox_event_failed(
                session, project_id=project_id, event_id=event_id, claim_attempt=attempt,
                error="native_departure_retry", retry_delay=timedelta(seconds=1),
            )
        async with system_project_session_scope(factory, project_id) as session:
            retry = await claim_next_outbox_event(
                session, project_id=project_id,
                claimed_at=datetime.now(UTC) + timedelta(seconds=2),
            )
            assert retry.id == event_id and retry.attempt_count == attempt + 1
            next_attempt = retry.attempt_count
        bot.get_chat_member.side_effect = None
        bot.get_chat_member.return_value = (
            ChatMemberLeft(user=user) if current_status == "left"
            else ChatMemberMember(user=user)
        )
        await process_claimed_native_departure(
            factory, project_id=project_id, event_id=event_id,
            claim_attempt=next_attempt, bot=bot,
        )
        async with system_project_session_scope(factory, project_id) as session:
            event = await session.get(OutboxEvent, event_id)
            assert event.status == "succeeded"
            state = await session.scalar(select(AccessState).where(
                AccessState.project_id == project_id, AccessState.resource_id == resource_id,
            ))
            assert state.actual_state == (
                "revoked" if current_status == "left" else "granted"
            )
    finally:
        await engine.dispose()


async def test_concurrent_native_departures_preserve_one_corrective_revision():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    base = datetime.now(UTC) - timedelta(minutes=2)
    try:
        owner_id = await _create_user(factory)
        project_id, resource_id = await _create_project_and_resource(factory, owner_id)
        context = TenantContext(user_id=owner_id, project_id=project_id)
        async with tenant_session_scope(factory, context) as session:
            await ensure_owner_service_access(
                session, project_id=project_id, resource_id=resource_id, granted_at=base,
            )
            resource = await session.get(Resource, resource_id)
            person = await session.get(TelegramUser, owner_id)
            chat_id, telegram_id = int(resource.external_id), person.telegram_user_id
        await run_one_telegram_access_action(
            factory, project_id=project_id, adapter=AlreadyMemberThenRevokeAdapter(),
            claimed_at=base, clock=lambda: base,
        )
        async def observe(moment):
            async with system_project_session_scope(factory, project_id) as session:
                return await reconcile_native_departure(
                    session, project_id=project_id, chat_id=chat_id,
                    telegram_user_id=telegram_id, observed_at=moment,
                )
        with pytest.raises(NativeObservationStale):
            await observe(base - timedelta(seconds=1))
        observed_at = datetime.now(UTC)
        results = await asyncio.gather(observe(observed_at), observe(observed_at))
        assert sorted(results) == [False, True]
        async with system_project_session_scope(factory, project_id) as session:
            state = await session.scalar(select(AccessState).where(
                AccessState.project_id == project_id, AccessState.resource_id == resource_id,
            ))
            action = await session.scalar(select(AccessAction).where(
                AccessAction.project_id == project_id, AccessAction.status == "pending",
            ))
            assert action.desired_revision == state.desired_revision
    finally:
        await engine.dispose()
