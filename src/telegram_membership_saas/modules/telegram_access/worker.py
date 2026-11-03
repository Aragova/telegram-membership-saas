from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiogram.exceptions import (
    AiogramError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.access import (
    AccessActionStatus,
    AccessActionType,
    claim_next_access_action,
    mark_access_action_failed,
    mark_access_action_succeeded,
)
from telegram_membership_saas.modules.telegram_access.adapter import (
    TelegramAccessAdapter,
    TelegramAccessCommand,
    TelegramAccessResult,
)
from telegram_membership_saas.modules.telegram_access.service import (
    build_telegram_access_command,
    close_pending_tickets_for_revoke,
    close_ticket_for_failed_delivery,
    record_grant_result,
)


@dataclass(frozen=True, slots=True)
class TelegramAccessExecution:
    action_id: UUID
    status: str
    ticket_id: UUID | None = None
    error: str | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


def _telegram_failure_policy(
    exc: Exception,
    *,
    moment: datetime,
    default_retry_delay: timedelta,
) -> tuple[bool, datetime | None]:
    if isinstance(exc, TelegramRetryAfter):
        return False, moment + timedelta(seconds=exc.retry_after)
    if isinstance(exc, (TelegramNetworkError, TelegramServerError)):
        return False, moment + default_retry_delay
    if isinstance(
        exc,
        (
            TelegramBadRequest,
            TelegramForbiddenError,
            TelegramNotFound,
            TelegramUnauthorizedError,
        ),
    ):
        return True, None
    if isinstance(exc, AiogramError):
        return False, moment + default_retry_delay
    return False, moment + default_retry_delay


async def _claim_command(
    session: AsyncSession,
    *,
    project_id: UUID,
    claimed_at: datetime,
    lease_timeout: timedelta,
) -> tuple[TelegramAccessCommand | None, TelegramAccessExecution | None]:
    action = await claim_next_access_action(
        session,
        project_id=project_id,
        claimed_at=claimed_at,
        lease_timeout=lease_timeout,
    )
    if action is None:
        return None, None

    try:
        return await build_telegram_access_command(session, action), None
    except Exception as exc:
        await mark_access_action_failed(
            session,
            project_id=project_id,
            action_id=action.id,
            claim_attempt=action.attempt_count,
            error=str(exc) or exc.__class__.__name__,
            failed_at=claimed_at,
            permanent=True,
        )
        return None, TelegramAccessExecution(
            action_id=action.id,
            status=AccessActionStatus.FAILED_PERMANENT.value,
            error=str(exc) or exc.__class__.__name__,
        )


async def _discard_quietly(
    adapter: TelegramAccessAdapter,
    result: TelegramAccessResult,
) -> None:
    try:
        await adapter.discard(result)
    except Exception:
        pass


async def _record_external_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    command: TelegramAccessCommand,
    exc: Exception,
    failed_at: datetime,
    default_retry_delay: timedelta,
    close_ticket: bool = False,
) -> TelegramAccessExecution:
    permanent, retry_at = _telegram_failure_policy(
        exc,
        moment=failed_at,
        default_retry_delay=default_retry_delay,
    )
    try:
        async with system_project_session_scope(session_factory, project_id) as session:
            if close_ticket:
                await close_ticket_for_failed_delivery(
                    session,
                    command=command,
                    closed_at=failed_at,
                )
            failed = await mark_access_action_failed(
                session,
                project_id=project_id,
                action_id=command.access.action_id,
                claim_attempt=command.access.claim_attempt,
                error=str(exc) or exc.__class__.__name__,
                failed_at=failed_at,
                retry_at=retry_at,
                permanent=permanent,
            )
        return TelegramAccessExecution(
            action_id=command.access.action_id,
            status=failed.status,
            error=str(exc) or exc.__class__.__name__,
        )
    except ValueError as stale:
        if "lease is no longer current" not in str(stale):
            raise
        return TelegramAccessExecution(
            action_id=command.access.action_id,
            status="stale_failure_ignored",
            error=str(exc) or exc.__class__.__name__,
        )


async def run_one_telegram_access_action(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    adapter: TelegramAccessAdapter,
    claimed_at: datetime | None = None,
    lease_timeout: timedelta | None = None,
    default_retry_delay: timedelta = timedelta(seconds=30),
    clock: Callable[[], datetime] | None = None,
) -> TelegramAccessExecution | None:
    effective_lease = lease_timeout or (adapter.invite_ttl + timedelta(minutes=5))
    if effective_lease.total_seconds() <= 0:
        raise ValueError("lease_timeout must be positive")
    if default_retry_delay.total_seconds() < 0:
        raise ValueError("default_retry_delay cannot be negative")

    current_time = clock or utcnow
    claim_moment = claimed_at or current_time()

    async with system_project_session_scope(session_factory, project_id) as session:
        command, terminal = await _claim_command(
            session,
            project_id=project_id,
            claimed_at=claim_moment,
            lease_timeout=effective_lease,
        )
    if terminal is not None:
        return terminal
    if command is None:
        return None

    try:
        result = await adapter.prepare(command)
    except Exception as exc:
        return await _record_external_failure(
            session_factory,
            project_id=project_id,
            command=command,
            exc=exc,
            failed_at=current_time(),
            default_retry_delay=default_retry_delay,
        )

    processed_at = current_time()
    if (
        processed_at >= claim_moment + effective_lease
        and command.access.action_type == AccessActionType.GRANT
        and result.invite_link is not None
    ):
        await _discard_quietly(adapter, result)
        return TelegramAccessExecution(
            action_id=command.access.action_id,
            status="lease_expired_before_persist",
        )

    if command.access.action_type == AccessActionType.GRANT and result.invite_link is not None:
        try:
            async with system_project_session_scope(session_factory, project_id) as session:
                ticket = await record_grant_result(
                    session,
                    command=command,
                    result=result,
                    recorded_at=processed_at,
                )
        except ValueError as stale:
            if "lease is no longer current" not in str(stale) and (
                "newer claim generation" not in str(stale)
            ):
                raise
            await _discard_quietly(adapter, result)
            return TelegramAccessExecution(
                action_id=command.access.action_id,
                status="stale_grant_result_ignored",
            )

        if ticket is None:
            raise RuntimeError("Telegram invite was prepared without a join ticket")

        try:
            await adapter.deliver(command, result)
        except Exception as exc:
            await _discard_quietly(adapter, result)
            return await _record_external_failure(
                session_factory,
                project_id=project_id,
                command=command,
                exc=exc,
                failed_at=current_time(),
                default_retry_delay=default_retry_delay,
                close_ticket=True,
            )

        return TelegramAccessExecution(
            action_id=command.access.action_id,
            status="awaiting_join",
            ticket_id=ticket.id,
        )

    completed_at = current_time()
    async with system_project_session_scope(session_factory, project_id) as session:
        if command.access.action_type == AccessActionType.REVOKE:
            await close_pending_tickets_for_revoke(
                session,
                command=command,
                closed_at=completed_at,
            )
        completed = await mark_access_action_succeeded(
            session,
            project_id=project_id,
            action_id=command.access.action_id,
            claim_attempt=command.access.claim_attempt,
            processed_at=completed_at,
        )

    return TelegramAccessExecution(
        action_id=command.access.action_id,
        status=completed.status,
    )
