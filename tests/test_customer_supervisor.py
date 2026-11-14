"""Safety contracts for automatic multi-project bot lifecycle."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.pool import NullPool

from telegram_membership_saas.config import Settings
from telegram_membership_saas.customer_supervisor import (
    BotTarget,
    CustomerBotSupervisor,
    run_customer_supervisor,
    safe_error_code,
)
from telegram_membership_saas.db import build_engine, build_lease_engine
from telegram_membership_saas.modules.bots.health_service import (
    public_health_label,
)
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth
from telegram_membership_saas.project_worker_lock import (
    ProjectWorkerBusy,
    project_lock_key,
)


def _target(pid=None, stamp=None):
    return BotTarget(pid or uuid4(), 123456789, stamp or datetime.now(UTC))


def _settings(**kwargs):
    return Settings(
        bot_token_encryption_key=SecretStr(Fernet.generate_key().decode()),
        telegram_central_access_enabled=False,
        _env_file=None,
        **kwargs,
    )


def test_project_lock_key_is_stable_and_project_specific():
    a, b = uuid4(), uuid4()
    assert project_lock_key(a) == project_lock_key(a)
    assert project_lock_key(a) != project_lock_key(b)
    assert -(2**63) <= project_lock_key(a) < 2**63


def test_only_recent_running_heartbeat_makes_owner_bot_link_live():
    pid = uuid4()
    now = datetime.now(UTC)
    health = CustomerBotRuntimeHealth(
        project_id=pid,
        telegram_bot_id=123456789,
        instance_id=uuid4(),
        state="running",
        last_heartbeat=now,
    )
    assert public_health_label(health, telegram_bot_id=123456789, now=now)[1] is True
    assert public_health_label(health, telegram_bot_id=999, now=now)[1] is False
    assert public_health_label(
        health, telegram_bot_id=123456789, now=now + timedelta(seconds=41)
    )[1] is False
    health.state = "error"
    health.error_code = "missing_admin_rights"
    assert "права" in public_health_label(
        health, telegram_bot_id=123456789, now=now
    )[0]
    assert public_health_label(health, telegram_bot_id=123456789, now=now)[1] is False


async def test_supervisor_requires_management_only_central_runtime():
    with pytest.raises(RuntimeError, match="Disable the legacy"):
        await run_customer_supervisor(Settings(
            bot_token_encryption_key=SecretStr(Fernet.generate_key().decode()),
            telegram_central_access_enabled=True,
            _env_file=None,
        ))


async def test_supervisor_keeps_enforcement_for_all_registered_projects(monkeypatch):
    from telegram_membership_saas import customer_supervisor as runtime

    pid, unready_pid = uuid4(), uuid4()
    a = _target(pid)
    b = _target(unready_pid)
    current = [a, b]
    never_ends = asyncio.Event()

    async def discover(_factory):
        return tuple(current)

    async def run(_engine, _factory, target, *, settings):
        await never_ends.wait()

    monkeypatch.setattr(runtime, "discover_targets", discover)
    monkeypatch.setattr(runtime, "discover_removal_targets", AsyncMock(return_value=()))
    monkeypatch.setattr(runtime, "run_target", run)

    supervisor = CustomerBotSupervisor(MagicMock(), MagicMock(), _settings())
    try:
        await supervisor.reconcile_once()
        await asyncio.sleep(0)
        # A project with no currently payable catalog still needs its
        # subscription-expiration and access-revocation worker.
        assert set(supervisor.workers) == {pid, unready_pid}

        # The original Telegram bot credentials rotate: old poller is cancelled.
        current = [_target(pid, a.credential_version + timedelta(seconds=1))]
        await supervisor.reconcile_once()
        assert supervisor.workers[pid].task.cancelling()
        assert supervisor.workers[unready_pid].task.cancelling()
        await asyncio.gather(
            *(w.task for w in supervisor.workers.values()),
            return_exceptions=True,
        )
        await supervisor.reconcile_once()
        # Reconciliation immediately starts the rotated bot with fresh identity.
        assert set(supervisor.workers) == {pid}
        assert supervisor.workers[pid].target == current[0]
        assert pid not in supervisor.retry_after

        await supervisor.reconcile_once()
        assert pid in supervisor.workers
        current = []
        await supervisor.reconcile_once()
        assert supervisor.workers[pid].task.cancelling()
    finally:
        for worker in supervisor.workers.values():
            worker.task.cancel()
        await asyncio.gather(
            *(x.task for x in supervisor.workers.values()),
            return_exceptions=True,
        )


def test_supervisor_never_reveals_token_in_safe_error_code():
    secret = "123456:" + "A" * 35
    error = RuntimeError("Telegram URL https://api.telegram.org/bot" + secret)
    assert safe_error_code(error) == "startup_failed"
    assert secret not in safe_error_code(error)
    assert safe_error_code(ProjectWorkerBusy("locked")) == "lock_lost"


async def test_stalled_telegram_poll_clears_live_health_before_unlock(monkeypatch):
    from telegram_membership_saas import customer_supervisor as runtime

    events = []
    target = _target()

    @asynccontextmanager
    async def lease(_engine, _project):
        events.append("acquired")
        try:
            yield MagicMock()
        finally:
            events.append("unlocked")

    async def fake_customer(
        _factory, *, project_id, telegram_bot_id, settings,
        on_poll_response, on_poll_failure
    ):
        await asyncio.sleep(0.03)
        on_poll_response()
        events.append("telegram_poll_ok")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("customer_stopped")

    async def health(*_args, **kwargs):
        events.append(kwargs["state"])

    monkeypatch.setattr(runtime, "project_worker_lock", lease)
    monkeypatch.setattr(runtime, "claim_bot_health", AsyncMock())
    monkeypatch.setattr(runtime, "confirm_project_lock", AsyncMock())
    monkeypatch.setattr(runtime, "run_customer_bot_instance", fake_customer)
    monkeypatch.setattr(runtime, "write_bot_health", health)
    monkeypatch.setattr(runtime, "HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(runtime, "POLLING_STALE_SECONDS", 0.09)

    await asyncio.wait_for(
        runtime.run_target(MagicMock(), MagicMock(), target, settings=_settings()),
        timeout=2,
    )
    assert "starting" in events
    assert "running" in events
    assert "error" in events
    assert events.index("customer_stopped") < events.index("unlocked")


async def test_repeated_cancel_never_releases_lease_before_slow_customer_teardown(
    monkeypatch,
):
    from telegram_membership_saas import customer_supervisor as runtime

    events = []
    target = _target()
    entered_cleanup = asyncio.Event()
    allow_cleanup = asyncio.Event()
    worker_started = asyncio.Event()

    @asynccontextmanager
    async def lease(_engine, _project):
        events.append("lease_acquired")
        try:
            yield MagicMock()
        finally:
            events.append("lease_released")

    async def slow_customer(
        _factory, *, project_id, telegram_bot_id, settings,
        on_poll_response, on_poll_failure
    ):
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("customer_cleanup_started")
            entered_cleanup.set()
            await allow_cleanup.wait()
            events.append("customer_cleanup_complete")

    monkeypatch.setattr(runtime, "project_worker_lock", lease)
    monkeypatch.setattr(runtime, "claim_bot_health", AsyncMock())
    monkeypatch.setattr(runtime, "confirm_project_lock", AsyncMock())
    monkeypatch.setattr(runtime, "run_customer_bot_instance", slow_customer)
    monkeypatch.setattr(runtime, "write_bot_health", AsyncMock())

    task = asyncio.create_task(
        runtime.run_target(MagicMock(), MagicMock(), target, settings=_settings())
    )
    try:
        await asyncio.wait_for(worker_started.wait(), timeout=2)
        task.cancel()
        await asyncio.wait_for(entered_cleanup.wait(), timeout=2)

        # A second cancellation arrives while the first is still draining.
        # It must not propagate into the slow customer cleanup or unlock DB.
        task.cancel()
        await asyncio.sleep(0)
        assert "lease_released" not in events
        assert "customer_cleanup_complete" not in events

        allow_cleanup.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
        assert events.index("customer_cleanup_complete") < events.index("lease_released")
    finally:
        allow_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_supervisor_does_not_send_second_cancel_to_stopping_worker(monkeypatch):
    from telegram_membership_saas import customer_supervisor as runtime

    target = _target()
    started = asyncio.Event()
    shutting_down = asyncio.Event()
    finish_shutdown = asyncio.Event()

    async def discovery(_factory):
        return (target,) if not shutting_down.is_set() else ()

    async def slow_worker(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            shutting_down.set()
            await finish_shutdown.wait()

    monkeypatch.setattr(runtime, "discover_targets", discovery)
    monkeypatch.setattr(runtime, "discover_removal_targets", AsyncMock(return_value=()))
    monkeypatch.setattr(runtime, "run_target", slow_worker)
    supervisor = CustomerBotSupervisor(MagicMock(), MagicMock(), _settings())
    await supervisor.reconcile_once()
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        shutting_down.set()
        await supervisor.reconcile_once()
        worker = supervisor.workers[target.project_id].task
        assert worker.cancelling() == 1
        await asyncio.sleep(0)

        # Further scans or shutdown must not cancel this task twice.
        await supervisor.reconcile_once()
        assert worker.cancelling() == 1

        finish_shutdown.set()
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=2)
        await supervisor.reconcile_once()
        assert target.project_id not in supervisor.workers
    finally:
        finish_shutdown.set()
        for running in supervisor.workers.values():
            if not running.task.done():
                running.task.cancel()
        await asyncio.gather(
            *(x.task for x in supervisor.workers.values()),
            return_exceptions=True,
        )


async def test_dedicated_advisory_engine_never_uses_operational_pool():
    settings = _settings()
    operational = build_engine(settings)
    lease = build_lease_engine(settings)
    try:
        assert lease is not operational
        assert isinstance(lease.pool, NullPool)
        assert not isinstance(operational.pool, NullPool)
    finally:
        await lease.dispose()
        await operational.dispose()


async def test_worker_admission_limit_preserves_operational_capacity(monkeypatch):
    from telegram_membership_saas import customer_supervisor as runtime

    candidates = [_target() for _ in range(5)]
    keep_alive = asyncio.Event()

    async def discover(_factory):
        return tuple(candidates)

    async def worker(_engine, _factory, _target, *, settings):
        await keep_alive.wait()

    statuses = AsyncMock()
    monkeypatch.setattr(runtime, "discover_targets", discover)
    monkeypatch.setattr(runtime, "discover_removal_targets", AsyncMock(return_value=()))
    monkeypatch.setattr(runtime, "run_target", worker)
    monkeypatch.setattr(runtime, "write_bot_health", statuses)

    supervisor = CustomerBotSupervisor(
        MagicMock(), MagicMock(), _settings(customer_supervisor_max_bots=2)
    )
    try:
        await supervisor.reconcile_once()
        await asyncio.sleep(0)
        assert len(supervisor.workers) == 2
        assert statuses.await_count == 3
        assert all(call.kwargs["state"] == "queued"
                   for call in statuses.await_args_list)

        # A second reconcile must not schedule an unbounded number of tasks.
        await supervisor.reconcile_once()
        assert len(supervisor.workers) == 2
    finally:
        for running in supervisor.workers.values():
            running.task.cancel()
        await asyncio.gather(
            *(x.task for x in supervisor.workers.values()),
            return_exceptions=True,
        )


def test_supervisor_capacity_configuration_rejects_unbounded_values():
    with pytest.raises(ValueError):
        _settings(customer_supervisor_max_bots=0)
    with pytest.raises(ValueError):
        _settings(customer_supervisor_max_bots=1000)
