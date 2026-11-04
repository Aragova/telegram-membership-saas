from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
)
from telegram_membership_saas.modules.access import enqueue_due_access_recalculations
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    claim_next_outbox_event,
    mark_outbox_event_failed,
    process_claimed_access_recalculate_event,
)
from telegram_membership_saas.modules.telegram_access import (
    TelegramAccessAdapter,
    ensure_owner_service_for_project,
    expire_due_join_tickets,
    run_one_telegram_access_action,
)
from telegram_membership_saas.project_worker_lock import (
    confirm_project_lock,
    project_worker_lock,
)
from telegram_membership_saas.task_lifecycle import cancel_once, complete_cleanup
from telegram_membership_saas.telegram_adapter import create_bot, create_dispatcher
from telegram_membership_saas.telegram_menus import install_public_commands

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


async def process_project_runtime_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    adapter: TelegramAccessAdapter,
    now: datetime | None = None,
) -> bool:
    moment = now or utcnow()
    did_work = False

    async with system_project_session_scope(session_factory, project_id) as session:
        boundaries = await enqueue_due_access_recalculations(
            session,
            project_id=project_id,
            now=moment,
        )
        expired_tickets = await expire_due_join_tickets(
            session,
            project_id=project_id,
            now=moment,
        )
        did_work = bool(boundaries or expired_tickets)

    claimed_event_id: UUID | None = None
    claimed_attempt = 0
    async with system_project_session_scope(session_factory, project_id) as session:
        event = await claim_next_outbox_event(
            session,
            project_id=project_id,
            claimed_at=moment,
        )
        if event is not None:
            claimed_event_id = event.id
            claimed_attempt = event.attempt_count

    if claimed_event_id is not None:
        did_work = True
        try:
            async with system_project_session_scope(session_factory, project_id) as session:
                event = await session.get(OutboxEvent, claimed_event_id)
                if event is None:
                    raise RuntimeError("Claimed outbox event disappeared")
                await process_claimed_access_recalculate_event(
                    session,
                    event=event,
                    claim_attempt=claimed_attempt,
                    processed_at=moment,
                )
        except Exception as exc:
            async with system_project_session_scope(session_factory, project_id) as session:
                try:
                    await mark_outbox_event_failed(
                        session,
                        project_id=project_id,
                        event_id=claimed_event_id,
                        claim_attempt=claimed_attempt,
                        error=str(exc) or exc.__class__.__name__,
                        failed_at=moment,
                        permanent=isinstance(exc, ValueError),
                        retry_delay=timedelta(seconds=30),
                    )
                except ValueError:
                    logger.warning(
                        "Outbox event %s could not be marked failed because its lease changed",
                        claimed_event_id,
                    )

    execution = await run_one_telegram_access_action(
        session_factory,
        project_id=project_id,
        adapter=adapter,
        claimed_at=moment,
        clock=lambda: utcnow(),
    )
    return did_work or execution is not None


async def reconcile_owner_service(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    now: datetime | None = None,
) -> int:
    moment = now or utcnow()
    async with system_project_session_scope(session_factory, project_id) as session:
        results = await ensure_owner_service_for_project(
            session,
            project_id=project_id,
            granted_at=moment,
        )
        return len(results)


async def run_project_runtime_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    adapter: TelegramAccessAdapter,
    poll_seconds: float = 1.0,
    owner_reconcile_seconds: float = 60.0,
    clock: Callable[[], datetime] | None = None,
) -> None:
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if owner_reconcile_seconds <= 0:
        raise ValueError("owner_reconcile_seconds must be positive")

    current_time = clock or utcnow
    next_owner_reconcile: datetime | None = None

    while True:
        moment = current_time()
        try:
            if next_owner_reconcile is None or moment >= next_owner_reconcile:
                await reconcile_owner_service(
                    session_factory,
                    project_id=project_id,
                    now=moment,
                )
                next_owner_reconcile = moment + timedelta(seconds=owner_reconcile_seconds)

            did_work = await process_project_runtime_once(
                session_factory,
                project_id=project_id,
                adapter=adapter,
                now=moment,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram project runtime iteration failed")
            did_work = False

        if not did_work:
            await asyncio.sleep(poll_seconds)


async def run_telegram_bot(settings: Settings | None = None) -> None:
    current = settings or get_settings()
    if current.telegram_central_access_enabled and current.telegram_project_id is None:
        raise RuntimeError(
            "TMS_TELEGRAM_PROJECT_ID is required while legacy central access is enabled"
        )

    engine = build_engine(current)
    factory = build_session_factory(engine)
    bot = create_bot(current)
    dispatcher = create_dispatcher(
        factory,
        project_id=current.telegram_project_id,
        enable_access=current.telegram_central_access_enabled,
    )
    worker: asyncio.Task[None] | None = None
    polling: asyncio.Task[None] | None = None
    guard: asyncio.Task[None] | None = None

    async def stop_dispatcher() -> None:
        if polling is not None:
            if not polling.done():
                await asyncio.sleep(0)
                try:
                    if not polling.done():
                        await dispatcher.stop_polling()
                except RuntimeError:
                    cancel_once(polling)
                except Exception:
                    cancel_once(polling)
            await asyncio.gather(polling, return_exceptions=True)

    try:
        await install_public_commands(bot, customer=False)
        if not current.telegram_central_access_enabled:
            # Central ViMi Pay must remain available for every new owner,
            # without a per-owner project UUID in deployment settings.
            polling = asyncio.create_task(
                dispatcher.start_polling(
                    bot, handle_signals=False, handle_as_tasks=False,
                    close_bot_session=False,
                )
            )
            try:
                await asyncio.shield(polling)
            finally:
                await complete_cleanup(stop_dispatcher())
        else:
            project_id = current.telegram_project_id
            assert project_id is not None
            # Release the project lock only *after* polling and access-worker
            # tasks have fully stopped. New workers can then safely take over.
            async with project_worker_lock(engine, project_id) as lease:
                try:
                    worker = asyncio.create_task(run_project_runtime_loop(
                        factory,
                        project_id=project_id,
                        adapter=TelegramAccessAdapter(bot, session_factory=factory),
                        poll_seconds=current.telegram_worker_poll_seconds,
                    ))

                    async def heartbeat() -> None:
                        while True:
                            await asyncio.sleep(3)
                            await confirm_project_lock(lease, project_id)

                    guard = asyncio.create_task(heartbeat())
                    polling = asyncio.create_task(
                        dispatcher.start_polling(
                            bot, handle_signals=False, handle_as_tasks=False,
                            close_bot_session=False,
                        )
                    )
                    done, _ = await asyncio.wait(
                        (guard, polling), return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                finally:
                    # Repeated termination signals must not interrupt cleanup.
                    # The lock is released only after both polling and access
                    # have stopped and all in-flight handlers were awaited.
                    async def shutdown() -> None:
                        try:
                            await stop_dispatcher()
                        finally:
                            cancel_once(guard)
                            cancel_once(worker)
                            await asyncio.gather(
                                *(task for task in (worker, guard) if task is not None),
                                return_exceptions=True,
                            )

                    await complete_cleanup(shutdown())
    finally:
        await bot.session.close()
        await engine.dispose()
