"""Durable, scoped membership observations; never a financial event."""
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.outbox import OutboxEvent, OutboxEventStatus, OutboxEventType
from telegram_membership_saas.modules.telegram_access.service import (
    NativeObservationStale,
    native_departure_target,
    reconcile_native_departure,
    require_aware,
)


class NativeDeparturePayloadError(ValueError):
    """A malformed durable event cannot be processed safely."""


def native_departure_failure_policy(
    exc: Exception, attempt: int,
) -> tuple[bool, timedelta, str]:
    """Return permanent flag, delay and safe code; preserve recoverable evidence."""
    if isinstance(exc, NativeDeparturePayloadError):
        return True, timedelta(0), "native_departure_invalid_payload"
    if isinstance(exc, TelegramRetryAfter):
        return False, timedelta(seconds=max(30, exc.retry_after)), "native_departure_rate_limited"
    if isinstance(exc, NativeObservationStale):
        return False, timedelta(seconds=30), "native_departure_stale_observation"
    if isinstance(exc, (
        TelegramForbiddenError, TelegramNotFound, TelegramUnauthorizedError, TelegramBadRequest,
    )):
        # Missing rights or a rotated token may recover. Do not discard paid
        # access evidence or hammer the Telegram API during a prolonged outage.
        return False, timedelta(hours=1), "native_departure_unavailable"
    delay = timedelta(seconds=30 * 2 ** min(max(attempt - 1, 0), 6))
    return False, delay, "native_departure_retry"


async def enqueue_native_departure(
    session: AsyncSession, *, project_id: UUID, chat_id: int, telegram_user_id: int,
    update_id: int, occurred_at: datetime,
) -> bool:
    require_aware(occurred_at)
    target = await native_departure_target(
        session, project_id=project_id, chat_id=chat_id, telegram_user_id=telegram_user_id,
    )
    if target is None:
        return False
    customer_id, resource_id = target
    result = await session.execute(
        insert(OutboxEvent).values(
            project_id=project_id,
            event_type=OutboxEventType.NATIVE_DEPARTURE.value,
            aggregate_type="resource",
            aggregate_id=resource_id,
            dedupe_key=f"native-departure:{resource_id}:{customer_id}:{update_id}:"
                       f"{int(occurred_at.timestamp())}",
            payload={"chat_id": str(chat_id), "telegram_user_id": str(telegram_user_id)},
            status=OutboxEventStatus.PENDING.value,
            available_at=datetime.now(UTC),
        ).on_conflict_do_nothing(index_elements=["project_id", "dedupe_key"])
        .returning(OutboxEvent.id)
    )
    return result.scalar_one_or_none() is not None


async def _current_event(
    session: AsyncSession, *, project_id: UUID, event_id: UUID, claim_attempt: int,
    lock: bool = False,
) -> OutboxEvent | None:
    statement = select(OutboxEvent).where(
        OutboxEvent.project_id == project_id, OutboxEvent.id == event_id,
        OutboxEvent.event_type == OutboxEventType.NATIVE_DEPARTURE.value,
        OutboxEvent.status == OutboxEventStatus.PROCESSING.value,
        OutboxEvent.attempt_count == claim_attempt,
    ).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def process_claimed_native_departure(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID, event_id: UUID,
    claim_attempt: int, bot: Bot,
) -> None:
    async with system_project_session_scope(factory, project_id) as session:
        event = await _current_event(
            session, project_id=project_id, event_id=event_id, claim_attempt=claim_attempt,
        )
        if event is None:
            return
        try:
            chat_id = int(event.payload["chat_id"])
            user_id = int(event.payload["telegram_user_id"])
        except (KeyError, TypeError, ValueError):
            raise NativeDeparturePayloadError("Invalid native departure target") from None
        if chat_id >= 0 or user_id <= 0:
            raise NativeDeparturePayloadError("Invalid native departure identity")
        target = await native_departure_target(
            session, project_id=project_id, chat_id=chat_id, telegram_user_id=user_id,
        )
        relevant = target is not None and target[1] == event.aggregate_id

    # No database transaction/row lock across Telegram I/O. Exceptions
    # propagate to the existing durable retry path (including FloodWait).
    observed_at = datetime.now(UTC)
    left = False
    if relevant:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        left = member.status == "left"

    async with system_project_session_scope(factory, project_id) as session:
        current = await _current_event(
            session, project_id=project_id, event_id=event_id,
            claim_attempt=claim_attempt, lock=True,
        )
        if current is None:
            return
        if left:
            target = await native_departure_target(
                session, project_id=project_id, chat_id=chat_id, telegram_user_id=user_id,
            )
            if target is not None and target[1] == current.aggregate_id:
                await reconcile_native_departure(
                    session, project_id=project_id, chat_id=chat_id,
                    telegram_user_id=user_id, observed_at=observed_at,
                )
        current.status = OutboxEventStatus.SUCCEEDED.value
        current.processed_at = datetime.now(UTC)
        current.locked_at = None
        current.last_error = None
        await session.flush()
