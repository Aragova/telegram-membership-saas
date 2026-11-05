"""Capacity-aware multi-shard supervisor: 100 projects, no duplicate pollers."""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from telegram_membership_saas.config import Settings
from telegram_membership_saas.customer_supervisor import (
    BotTarget,
    CustomerBotSupervisor,
    assign_project_shards,
    ensure_supervisor_database_budget,
)
from telegram_membership_saas.project_worker_lock import ProjectWorkerBusy


def targets(total: int) -> tuple[BotTarget, ...]:
    return tuple(
        BotTarget(
            project_id=UUID(int=index + 1),
            telegram_bot_id=100000000 + index,
            credential_version=datetime.now(UTC),
        )
        for index in range(total)
    )


def configuration(shard_id: int, *, count: int = 4, cap: int = 32) -> Settings:
    return Settings(
        customer_supervisor_shard_count=count,
        customer_supervisor_shard_id=shard_id,
        customer_supervisor_max_bots=cap,
        _env_file=None,
    )


def test_exactly_100_bots_are_assigned_with_capacity_headroom():
    pool = targets(100)
    first = assign_project_shards(pool, shard_count=4, shard_capacity=32)
    backwards = assign_project_shards(
        tuple(reversed(pool)), shard_count=4, shard_capacity=32,
    )
    assert first == backwards
    assert len(first) == 100
    assert all(owner is not None for owner in first.values())
    assert all(sum(owner == shard for owner in first.values()) <= 32
               for shard in range(4))
    assert sum(sum(owner == shard for owner in first.values())
               for shard in range(4)) == 100


def test_capacity_overflow_has_exactly_one_global_queue():
    pool = targets(131)
    placement = assign_project_shards(pool, shard_count=4, shard_capacity=32)
    assert sum(shard is not None for shard in placement.values()) == 128
    assert sum(shard is None for shard in placement.values()) == 3


def test_shard_configuration_fails_closed_and_preserves_old_default():
    original = Settings(_env_file=None)
    assert original.customer_supervisor_shard_count == 1
    assert original.customer_supervisor_shard_id == 0
    assert original.customer_supervisor_max_bots == 10
    for kwargs in (
        {"customer_supervisor_shard_count": 0},
        {"customer_supervisor_shard_count": 4,
         "customer_supervisor_shard_id": 4},
        {"customer_supervisor_shard_id": 1},
        {"customer_supervisor_max_bots": 0},
        {"customer_supervisor_max_bots": 100},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **kwargs)


async def test_four_supervisors_schedule_100_distinct_projects(monkeypatch):
    from telegram_membership_saas import customer_supervisor as runtime

    pool = targets(100)
    release = asyncio.Event()

    async def discover(_factory):
        return pool

    async def worker(_engine, _factory, _target, *, settings):
        await release.wait()

    queued = AsyncMock()
    monkeypatch.setattr(runtime, "discover_targets", discover)
    monkeypatch.setattr(runtime, "discover_removal_targets",
                        AsyncMock(return_value=()))
    monkeypatch.setattr(runtime, "run_target", worker)
    monkeypatch.setattr(runtime, "write_bot_health", queued)

    supervisors = [
        CustomerBotSupervisor(MagicMock(), MagicMock(), configuration(shard))
        for shard in range(4)
    ]
    try:
        await asyncio.gather(*(s.reconcile_once() for s in supervisors))
        assert len(set().union(*(set(s.workers) for s in supervisors))) == 100
        assert sum(len(s.workers) for s in supervisors) == 100
        assert all(len(s.workers) <= 32 for s in supervisors)
        queued.assert_not_awaited()
        # On subsequent discovery, no duplicate workers are created.
        await asyncio.gather(*(s.reconcile_once() for s in supervisors))
        assert sum(len(s.workers) for s in supervisors) == 100
    finally:
        release.set()
        await asyncio.gather(
            *(worker.task for s in supervisors for worker in s.workers.values()),
            return_exceptions=True,
        )


async def test_capacity_queue_is_written_by_one_replica(monkeypatch):
    from telegram_membership_saas import customer_supervisor as runtime

    pool = targets(131)
    release = asyncio.Event()

    async def worker(_engine, _factory, _target, *, settings):
        await release.wait()

    statuses = AsyncMock()
    monkeypatch.setattr(runtime, "discover_targets", AsyncMock(return_value=pool))
    monkeypatch.setattr(runtime, "discover_removal_targets",
                        AsyncMock(return_value=()))
    monkeypatch.setattr(runtime, "run_target", worker)
    monkeypatch.setattr(runtime, "write_bot_health", statuses)

    supervisors = [
        CustomerBotSupervisor(MagicMock(), MagicMock(), configuration(shard))
        for shard in range(4)
    ]
    try:
        await asyncio.gather(*(s.reconcile_once() for s in supervisors))
        assert sum(len(s.workers) for s in supervisors) == 128
        assert statuses.await_count == 3
        assert all(x.kwargs["state"] == "queued"
                   for x in statuses.await_args_list)
    finally:
        release.set()
        await asyncio.gather(
            *(worker.task for s in supervisors for worker in s.workers.values()),
            return_exceptions=True,
        )


@pytest.mark.parametrize(("max_connections", "allowed"), [(240, True), (100, False)])
async def test_scaled_supervisor_checks_database_connection_headroom(
    max_connections, allowed,
):
    db_session = MagicMock()
    db_session.scalar = AsyncMock(return_value=str(max_connections))
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db_session)

    if allowed:
        await ensure_supervisor_database_budget(factory, configuration(0))
    else:
        with pytest.raises(RuntimeError, match="PostgreSQL connection budget"):
            await ensure_supervisor_database_budget(factory, configuration(0))
    db_session.scalar.assert_awaited_once()
    single = Settings(_env_file=None)
    factory.reset_mock()
    await ensure_supervisor_database_budget(factory, single)
    factory.assert_not_called()


async def test_legacy_single_shard_never_evicts_incumbent_on_new_registration(
    monkeypatch,
):
    from telegram_membership_saas import customer_supervisor as runtime

    initial = targets(2)
    newcomer = BotTarget(UUID(int=0), 200000000, datetime.now(UTC))
    discovered = list(initial)
    release = asyncio.Event()

    async def find(_factory):
        return tuple(discovered)

    async def worker(_engine, _factory, _target, *, settings):
        await release.wait()

    statuses = AsyncMock()
    monkeypatch.setattr(runtime, "discover_targets", find)
    monkeypatch.setattr(runtime, "discover_removal_targets",
                        AsyncMock(return_value=()))
    monkeypatch.setattr(runtime, "run_target", worker)
    monkeypatch.setattr(runtime, "write_bot_health", statuses)

    supervisor = CustomerBotSupervisor(
        MagicMock(), MagicMock(), configuration(0, count=1, cap=2),
    )
    try:
        await supervisor.reconcile_once()
        assert set(supervisor.workers) == {t.project_id for t in initial}
        discovered.append(newcomer)
        await supervisor.reconcile_once()
        assert set(supervisor.workers) == {t.project_id for t in initial}
        assert all(not worker.task.cancelling()
                   for worker in supervisor.workers.values())
        assert statuses.await_count == 1
        assert statuses.await_args.kwargs["state"] == "queued"
        assert statuses.await_args.kwargs["project_id"] == newcomer.project_id
    finally:
        release.set()
        await asyncio.gather(
            *(w.task for w in supervisor.workers.values()),
            return_exceptions=True,
        )


async def test_initial_busy_lease_has_no_failure_backoff(monkeypatch):
    from telegram_membership_saas import customer_supervisor as runtime

    target = targets(1)[0]
    release = asyncio.Event()
    attempts = 0

    async def worker(_engine, _factory, _target, *, settings):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProjectWorkerBusy("old shard is still draining")
        await release.wait()

    monkeypatch.setattr(runtime, "discover_targets",
                        AsyncMock(return_value=(target,)))
    monkeypatch.setattr(runtime, "discover_removal_targets",
                        AsyncMock(return_value=()))
    monkeypatch.setattr(runtime, "run_target", worker)

    supervisor = CustomerBotSupervisor(
        MagicMock(), MagicMock(), configuration(0, count=1, cap=1),
    )
    try:
        await supervisor.reconcile_once()
        await asyncio.sleep(0)
        assert attempts == 1
        await supervisor.reconcile_once()
        await asyncio.sleep(0)
        assert attempts == 2
        assert target.project_id not in supervisor.retry_after
    finally:
        release.set()
        await asyncio.gather(
            *(w.task for w in supervisor.workers.values()),
            return_exceptions=True,
        )
