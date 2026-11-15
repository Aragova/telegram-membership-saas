"""Existing-member notifications are best-effort and follow committed access."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.access import (
    AccessActionStatus,
    AccessActionType,
    AccessCommand,
)
from telegram_membership_saas.modules.telegram_access import worker as access_worker
from telegram_membership_saas.modules.telegram_access.adapter import (
    TelegramAccessCommand,
    TelegramAccessResult,
)


@pytest.mark.parametrize(
    ("desired", "already_processed", "delivery_fails", "expected_notices"),
    [
        (True, False, False, 1),
        (True, False, True, 1),
        (False, False, False, 0),
        (True, True, False, 0),
    ],
)
async def test_existing_member_notice_only_after_current_grant_is_committed(
    monkeypatch, desired, already_processed, delivery_fails, expected_notices,
) -> None:
    moment = datetime(2026, 11, 15, 12, tzinfo=UTC)
    project_id = uuid4()
    command = TelegramAccessCommand(
        access=AccessCommand(
            action_id=uuid4(),
            project_id=project_id,
            customer_id=uuid4(),
            resource_id=uuid4(),
            action_type=AccessActionType.GRANT,
            claim_attempt=1,
        ),
        telegram_user_id=123456789,
        chat_id=-100123456789,
    )
    events: list[str] = []
    session = SimpleNamespace(scalar=AsyncMock(return_value=desired))

    @asynccontextmanager
    async def fake_scope(*_args, **_kwargs):
        try:
            yield session
        finally:
            events.append("committed")

    class ExistingMemberAdapter:
        invite_ttl = timedelta(minutes=15)

        async def prepare(self, _command):
            return TelegramAccessResult(
                already_member=True,
                resolved_chat_id=-100123456789,
            )

        async def deliver(self, _command, _result):
            events.append("notified")
            if delivery_fails:
                raise RuntimeError("notification unavailable")

    monkeypatch.setattr(access_worker, "system_project_session_scope", fake_scope)
    monkeypatch.setattr(
        access_worker, "_claim_command", AsyncMock(return_value=(command, None)),
    )
    previous = moment - timedelta(seconds=1)
    completed = SimpleNamespace(
        status=AccessActionStatus.SUCCEEDED.value,
        processed_at=previous if already_processed else moment,
    )
    mark_succeeded = AsyncMock(return_value=completed)
    monkeypatch.setattr(access_worker, "mark_access_action_succeeded", mark_succeeded)

    result = await access_worker.run_one_telegram_access_action(
        object(),
        project_id=project_id,
        adapter=ExistingMemberAdapter(),  # type: ignore[arg-type]
        claimed_at=moment,
        clock=lambda: moment,
    )

    assert result is not None
    assert result.status == AccessActionStatus.SUCCEEDED.value
    assert events.count("notified") == expected_notices
    assert events.count("committed") == 2
    if expected_notices:
        assert events[-1] == "notified"
    mark_succeeded.assert_awaited_once()
    assert session.scalar.await_count == (0 if already_processed else 1)


async def test_invite_and_revoke_paths_do_not_use_existing_member_notification(
    monkeypatch,
) -> None:
    moment = datetime(2026, 11, 15, 12, tzinfo=UTC)
    project_id = uuid4()
    deliveries: list[str] = []

    @asynccontextmanager
    async def fake_scope(*_args, **_kwargs):
        yield SimpleNamespace()

    for action_type in (AccessActionType.GRANT, AccessActionType.REVOKE):
        command = TelegramAccessCommand(
            access=AccessCommand(
                action_id=uuid4(),
                project_id=project_id,
                customer_id=uuid4(),
                resource_id=uuid4(),
                action_type=action_type,
                claim_attempt=1,
            ),
            telegram_user_id=123456789,
            chat_id=-100123456789,
        )

        class Adapter:
            invite_ttl = timedelta(minutes=15)

            async def prepare(self, _command):
                return TelegramAccessResult()

            async def deliver(self, _command, _result):
                deliveries.append("unexpected")

        monkeypatch.setattr(access_worker, "system_project_session_scope", fake_scope)
        monkeypatch.setattr(
            access_worker, "_claim_command", AsyncMock(return_value=(command, None)),
        )
        monkeypatch.setattr(
            access_worker, "mark_access_action_succeeded",
            AsyncMock(return_value=SimpleNamespace(
                status=AccessActionStatus.SUCCEEDED.value,
                processed_at=moment,
            )),
        )
        monkeypatch.setattr(
            access_worker, "close_pending_tickets_for_revoke", AsyncMock(),
        )

        result = await access_worker.run_one_telegram_access_action(
            object(),
            project_id=project_id,
            adapter=Adapter(),  # type: ignore[arg-type]
            claimed_at=moment,
            clock=lambda: moment,
        )
        assert result is not None
        assert result.status == AccessActionStatus.SUCCEEDED.value

    assert deliveries == []
