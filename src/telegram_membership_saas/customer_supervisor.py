"""Automatically run one eligible branded customer bot per project.

Discovery exposes only a narrow SECURITY DEFINER projection, not credentials.
Each worker loads its own ciphertext inside a project-scoped RLS session.
PostgreSQL advisory locks prevent duplicate access execution across replicas.
"""

from __future__ import annotations

import asyncio
import logging
from hashlib import blake2s
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.customer_runtime import run_customer_bot_instance
from telegram_membership_saas.db import (
    build_engine,
    build_lease_engine,
    build_session_factory,
)
from telegram_membership_saas.modules.bots.deletion_service import (
    finalize_bot_retirement,
)
from telegram_membership_saas.modules.bots.health_service import (
    claim_bot_health,
    write_bot_health,
)
from telegram_membership_saas.project_worker_lock import (
    ProjectWorkerBusy,
    confirm_project_lock,
    project_worker_lock,
)
from telegram_membership_saas.task_lifecycle import cancel_once, complete_cleanup

logger = logging.getLogger(__name__)
SCAN_SECONDS = 10
HEARTBEAT_SECONDS = 3
FAILURE_BACKOFF_SECONDS = 45
POLLING_STALE_SECONDS = 45
# Each supervisor owns one operational pool (5 + 10 possible overflow connections).
# Leave additional PostgreSQL slots for the central bot, API and maintenance.
OPERATIONAL_POOL_BUDGET_PER_SHARD = 15
OTHER_DATABASE_CONNECTION_RESERVE = 40
POSTGRES_SUPERUSER_RESERVED_CONNECTIONS = 3


@dataclass(frozen=True, slots=True)
class BotTarget:
    project_id: UUID
    telegram_bot_id: int
    credential_version: datetime


@dataclass(slots=True)
class RunningTarget:
    target: BotTarget
    task: asyncio.Task[None]


def assign_project_shards(
    targets: tuple[BotTarget, ...], *, shard_count: int, shard_capacity: int,
) -> dict[UUID, int | None]:
    """Deterministic, capacity-bounded placement across independent workers.

    All replicas observe the same registered targets and compute the same
    assignments. Rendezvous preference keeps most projects on their preferred
    shard; overflow goes to the next available shard. A project moves only
    when the shared target set or deployment topology changes, and the
    existing per-project PostgreSQL lease prevents concurrent pollers.
    None means the overall installation is genuinely out of capacity.
    """
    if shard_count < 1 or shard_capacity < 1:
        raise ValueError("Invalid supervisor shard topology")
    load = [0] * shard_count
    placements: dict[UUID, int | None] = {}
    for target in sorted(targets, key=lambda item: item.project_id.int):
        if target.project_id in placements:
            raise ValueError("Duplicate project in customer supervisor discovery")
        ranked = sorted(
            range(shard_count),
            key=lambda shard: blake2s(
                b"vimi-supervisor-shard-v1:"
                + target.project_id.bytes + shard.to_bytes(2, "big"),
                digest_size=8,
            ).digest(),
            reverse=True,
        )
        assignment = next(
            (shard for shard in ranked if load[shard] < shard_capacity),
            None,
        )
        placements[target.project_id] = assignment
        if assignment is not None:
            load[assignment] += 1
    return placements


async def ensure_supervisor_database_budget(
    factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> None:
    """Fail closed before starting multi-shard polling on an undersized DB.

    With a single shard we preserve the existing staging configuration.
    A multi-shard rollout must reserve one PostgreSQL lease connection per
    possible worker, operational pools for each shard, and spare connections
    for the API/central bot. This is a conservative admission check, not a
    statement about RAM, CPU, disk or Telegram throughput.
    """
    if settings.customer_supervisor_shard_count == 1:
        return
    expected = (
        settings.customer_supervisor_shard_count
        * (settings.customer_supervisor_max_bots + OPERATIONAL_POOL_BUDGET_PER_SHARD)
        + OTHER_DATABASE_CONNECTION_RESERVE
        + POSTGRES_SUPERUSER_RESERVED_CONNECTIONS
    )
    async with factory() as session:
        configured = int(await session.scalar(text("SHOW max_connections")))
    if configured < expected:
        raise RuntimeError(
            "PostgreSQL connection budget is too small for the requested "
            f"supervisor topology: max_connections={configured}; "
            f"at least {expected} required before enabling multiple shards"
        )


async def discover_targets(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[BotTarget, ...]:
    """Only callable by the trusted runtime DB role; never returns token data."""
    async with factory() as session:
        rows = (await session.execute(
            text(
                "SELECT project_id, telegram_bot_id, credential_version "
                "FROM public.list_customer_bot_targets()"
            )
        )).all()
    return tuple(BotTarget(*row) for row in rows)


async def discover_removal_targets(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[tuple[UUID, int], ...]:
    """Discover retirement requests without exposing any stored credentials."""
    async with factory() as session:
        rows = (await session.execute(
            text(
                "SELECT project_id, telegram_bot_id "
                "FROM public.list_customer_bot_removals()"
            )
        )).all()
    return tuple((project_id, bot_id) for project_id, bot_id in rows)


def safe_error_code(exc: Exception) -> str:
    # Never persist/print underlying Telegram transport exception messages:
    # they may contain a URL with the bot token.
    if isinstance(exc, ProjectWorkerBusy):
        return "lock_lost"
    message = str(exc)
    if "credentials" in message or "credential" in message:
        return "invalid_credentials"
    if "administrator rights" in message:
        return "missing_admin_rights"
    if "identity or webhook" in message or "polling stalled" in message:
        return "telegram_check_failed"
    return "startup_failed"


async def run_target(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    target: BotTarget,
    *,
    settings: Settings,
) -> None:
    project_id = target.project_id
    bot_id = target.telegram_bot_id
    instance_id = uuid4()
    status = "stopped"
    error_code: str | None = None
    acquired = False
    try:
        async with project_worker_lock(engine, project_id) as lease:
            acquired = True
            status = "starting"
            await claim_bot_health(
                lease, project_id=project_id, bot_id=bot_id,
                instance_id=instance_id,
            )
            # Aiogram retries getUpdates errors internally without ending the
            # polling task. Only successful getUpdates, including empty
            # responses, prove real Telegram reachability.
            booted_at = monotonic()
            last_poll_response: float | None = None

            def observed_poll_response() -> None:
                nonlocal last_poll_response
                last_poll_response = monotonic()

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(HEARTBEAT_SECONDS)
                    await confirm_project_lock(lease, project_id)
                    last_ok = last_poll_response
                    silence = monotonic() - (
                        last_ok if last_ok is not None else booted_at
                    )
                    if silence > POLLING_STALE_SECONDS:
                        raise RuntimeError("Customer Telegram polling stalled")
                    await write_bot_health(
                        factory, project_id=project_id, bot_id=bot_id,
                        instance_id=instance_id,
                        state="running" if last_ok is not None else "starting",
                    )

            customer = asyncio.create_task(run_customer_bot_instance(
                factory, project_id=project_id, telegram_bot_id=bot_id,
                settings=settings, on_poll_response=observed_poll_response,
            ))
            guard = asyncio.create_task(heartbeat())
            try:
                done, _ = await asyncio.wait(
                    (customer, guard), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
                if guard in done:
                    raise ProjectWorkerBusy("Customer project lease was lost")
                error_code = "worker_stopped"
                status = "error"
            finally:
                async def shutdown() -> None:
                    cancel_once(customer)
                    cancel_once(guard)
                    await asyncio.gather(
                        customer, guard, return_exceptions=True
                    )

                # Do not release the lease even if the supervisor cancels this
                # target again while aiogram is draining its GetUpdates task.
                await complete_cleanup(shutdown())
    except asyncio.CancelledError:
        # Configuration switch, token rotation or graceful shutdown.
        status = "stopped"
        raise
    except ProjectWorkerBusy:
        # Another replica (or the old staging worker) owns this project.
        # Never write a competing status record when we did not get the lease.
        if not acquired:
            return
        error_code = "lock_lost"
        status = "error"
    except Exception as exc:
        status = "error"
        error_code = safe_error_code(exc)
        logger.warning("Customer worker stopped: project=%s reason=%s", project_id, error_code)
    finally:
        if acquired:
            try:
                await write_bot_health(
                    factory, project_id=project_id, bot_id=bot_id,
                    instance_id=instance_id, state=status, error_code=error_code,
                )
            except Exception:
                logger.warning("Could not persist customer worker shutdown state")


class CustomerBotSupervisor:
    def __init__(
        self,
        engine: AsyncEngine,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.engine = engine
        self.factory = factory
        self.settings = settings
        self.workers: dict[UUID, RunningTarget] = {}
        self.retry_after: dict[UUID, float] = {}
        self._supervisor_id = uuid4()

    async def reconcile_once(self) -> None:
        discovered = await discover_targets(self.factory)
        placement = assign_project_shards(
            discovered,
            shard_count=self.settings.customer_supervisor_shard_count,
            shard_capacity=self.settings.customer_supervisor_max_bots,
        )
        # Only the owning shard starts a project's poller. Over-capacity bots
        # are reported as queued by shard 0 once, never by every replica.
        ready = {
            t.project_id: t for t in discovered
            if placement[t.project_id] == self.settings.customer_supervisor_shard_id
        }

        for pid, current in list(self.workers.items()):
            if current.task.done():
                was_cancelled = current.task.cancelled()
                await asyncio.gather(current.task, return_exceptions=True)
                del self.workers[pid]
                if was_cancelled:
                    # Config changes and token rotations should restart promptly.
                    self.retry_after.pop(pid, None)
                else:
                    self.retry_after[pid] = monotonic() + FAILURE_BACKOFF_SECONDS
            elif pid not in ready or ready[pid] != current.target:
                cancel_once(current.task)

        # Each running project consumes one dedicated PostgreSQL advisory-lock
        # connection. Never admit more than the configured safe DB capacity.
        # Retiring bots have already disappeared from registered targets.
        # Polling must be fully drained and the advisory lock free first.
        await self.finalize_removals()

        if self.settings.customer_supervisor_shard_id == 0:
            for target in discovered:
                if placement[target.project_id] is None:
                    await write_bot_health(
                        self.factory,
                        project_id=target.project_id,
                        bot_id=target.telegram_bot_id,
                        instance_id=self._supervisor_id,
                        state="queued",
                    )

        for pid, target in sorted(ready.items(), key=lambda item: item[0].int):
            if pid in self.workers or monotonic() < self.retry_after.get(pid, 0):
                continue
            if len(self.workers) >= self.settings.customer_supervisor_max_bots:
                # Do not overwrite a healthy status owned by another replica.
                await write_bot_health(
                    self.factory,
                    project_id=pid,
                    bot_id=target.telegram_bot_id,
                    instance_id=self._supervisor_id,
                    state="queued",
                )
                continue
            task = asyncio.create_task(
                run_target(self.engine, self.factory, target, settings=self.settings)
            )
            self.workers[pid] = RunningTarget(target=target, task=task)
            logger.info("Customer runtime scheduled for project %s", pid)

    async def finalize_removals(self) -> None:
        """Remove credentials only after the project's poller has stopped."""
        pending = await discover_removal_targets(self.factory)
        for project_id, bot_id in pending:
            if project_id in self.workers:
                continue
            try:
                async with project_worker_lock(self.engine, project_id):
                    await finalize_bot_retirement(
                        self.factory,
                        project_id=project_id,
                        telegram_bot_id=bot_id,
                    )
            except ProjectWorkerBusy:
                # Another supervisor (or old bot instance) owns this lease.
                continue
            except Exception:
                logger.warning(
                    "Bot retirement will be retried for project %s", project_id
                )

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self.reconcile_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Customer supervisor discovery failed; retrying")
                await asyncio.sleep(SCAN_SECONDS)
        finally:
            async def shutdown() -> None:
                for worker in self.workers.values():
                    cancel_once(worker.task)
                await asyncio.gather(
                    *(w.task for w in self.workers.values()),
                    return_exceptions=True,
                )
                self.workers.clear()

            await complete_cleanup(shutdown())


async def run_customer_supervisor(settings: Settings | None = None) -> None:
    current = settings or get_settings()
    if current.bot_token_encryption_key is None:
        raise RuntimeError("Customer bot encryption key is not configured")
    if current.telegram_central_access_enabled:
        # All customer access work must be owned by the same customer runtime
        # identity after the one-time platform cutover. Never silently allow
        # two workers for the project's old access queue.
        raise RuntimeError(
            "Disable the legacy central customer access worker before launching supervisor"
        )
    # Isolation is essential: the long-lived advisory-lock connections must
    # never consume the pool used by payments, access revocation or discovery.
    operational_engine = build_engine(current)
    lease_engine = build_lease_engine(current)
    try:
        await ensure_supervisor_database_budget(
            build_session_factory(operational_engine), current,
        )
        supervisor = CustomerBotSupervisor(
            lease_engine,
            build_session_factory(operational_engine),
            current,
        )
        await supervisor.run()
    finally:
        async def shutdown_engines() -> None:
            try:
                await lease_engine.dispose()
            finally:
                await operational_engine.dispose()

        await complete_cleanup(shutdown_engines())
