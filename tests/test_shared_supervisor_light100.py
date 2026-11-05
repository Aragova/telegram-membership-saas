"""Light 100-bot profile admission, defaults, and connection budget."""
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
    ensure_supervisor_database_budget,
)


def config(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_shared_leases_must_be_explicit():
    original = config()
    assert original.customer_supervisor_lease_mode == "per_project"
    assert original.customer_supervisor_max_bots == 10
    with pytest.raises(ValidationError):
        config(customer_supervisor_max_bots=100)
    enabled = config(
        customer_supervisor_lease_mode="shared",
        customer_supervisor_max_bots=100,
    )
    assert enabled.customer_supervisor_shard_count == 1
    assert enabled.customer_supervisor_max_bots == 100


@pytest.mark.parametrize(
    ("connection_cap", "should_pass"), [(100, True), (58, False)],
)
async def test_shared_lease_database_budget_supports_existing_vm(
    connection_cap, should_pass,
):
    db_session = MagicMock()
    db_session.scalar = AsyncMock(return_value=str(connection_cap))
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db_session)
    settings = config(
        customer_supervisor_lease_mode="shared",
        customer_supervisor_max_bots=100,
    )
    if should_pass:
        await ensure_supervisor_database_budget(factory, settings)
    else:
        with pytest.raises(RuntimeError, match="PostgreSQL connection budget"):
            await ensure_supervisor_database_budget(factory, settings)
    db_session.scalar.assert_awaited_once()


async def test_shared_mode_starts_100_bots_in_five_per_scan_batches(monkeypatch):
    from telegram_membership_saas import customer_supervisor as runtime

    discovered = tuple(
        BotTarget(
            project_id=UUID(int=i + 1),
            telegram_bot_id=2000000 + i,
            credential_version=datetime.now(UTC),
        )
        for i in range(100)
    )
    release = asyncio.Event()

    async def worker(_engine, _factory, _target, *, settings):
        await release.wait()

    monkeypatch.setattr(
        runtime, "discover_targets", AsyncMock(return_value=discovered),
    )
    monkeypatch.setattr(
        runtime, "discover_removal_targets", AsyncMock(return_value=()),
    )
    monkeypatch.setattr(runtime, "run_target", worker)
    queued = AsyncMock()
    monkeypatch.setattr(runtime, "write_bot_health", queued)

    settings = config(
        customer_supervisor_lease_mode="shared",
        customer_supervisor_max_bots=100,
    )
    supervisor = CustomerBotSupervisor(MagicMock(), MagicMock(), settings)
    try:
        await supervisor.reconcile_once()
        assert len(supervisor.workers) == 5
        for _ in range(19):
            await supervisor.reconcile_once()
        assert len(supervisor.workers) == 100
        assert len(set(supervisor.workers)) == 100
        queued.assert_not_awaited()
        await supervisor.reconcile_once()
        assert len(supervisor.workers) == 100
    finally:
        release.set()
        await asyncio.gather(
            *(w.task for w in supervisor.workers.values()),
            return_exceptions=True,
        )


async def test_failed_shared_lease_stops_supervisor_for_clean_restart():
    from telegram_membership_saas.project_worker_lock import SharedProjectLeaseManager

    manager = SharedProjectLeaseManager(MagicMock())
    manager._broken = True
    supervisor = CustomerBotSupervisor(
        manager, MagicMock(),
        config(customer_supervisor_lease_mode="shared",
               customer_supervisor_max_bots=100),
    )
    supervisor.reconcile_once = AsyncMock()
    with pytest.raises(RuntimeError, match="restart required"):
        await asyncio.wait_for(supervisor.run(), timeout=1)
    supervisor.reconcile_once.assert_awaited_once()


async def test_busy_first_five_projects_do_not_starve_other_bot_startups(
    monkeypatch,
):
    from telegram_membership_saas import customer_supervisor as runtime
    from telegram_membership_saas.project_worker_lock import ProjectWorkerBusy

    pool = tuple(
        BotTarget(
            project_id=UUID(int=i + 1),
            telegram_bot_id=4000000 + i,
            credential_version=datetime.now(UTC),
        )
        for i in range(8)
    )
    attempts = []
    release = asyncio.Event()

    async def worker(_engine, _factory, target, *, settings):
        attempts.append(target.project_id.int)
        if target.project_id.int <= 5:
            raise ProjectWorkerBusy("Old supervisor still draining")
        await release.wait()

    monkeypatch.setattr(runtime, "discover_targets",
                        AsyncMock(return_value=pool))
    monkeypatch.setattr(runtime, "discover_removal_targets",
                        AsyncMock(return_value=()))
    monkeypatch.setattr(runtime, "run_target", worker)
    monkeypatch.setattr(runtime, "write_bot_health", AsyncMock())
    supervisor = CustomerBotSupervisor(
        MagicMock(), MagicMock(),
        config(customer_supervisor_lease_mode="shared",
               customer_supervisor_max_bots=8),
    )
    try:
        await supervisor.reconcile_once()
        await asyncio.sleep(0)
        assert attempts == [1, 2, 3, 4, 5]
        await supervisor.reconcile_once()
        await asyncio.sleep(0)
        assert {6, 7, 8}.issubset(set(attempts))
    finally:
        release.set()
        await asyncio.gather(
            *(w.task for w in supervisor.workers.values()),
            return_exceptions=True,
        )
