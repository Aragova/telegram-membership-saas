"""Safety contracts for automatic multi-project bot lifecycle."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from telegram_membership_saas.config import Settings
from telegram_membership_saas.customer_supervisor import (
    BotTarget,
    CustomerBotSupervisor,
    run_customer_supervisor,
    safe_error_code,
)
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


async def test_supervisor_only_launches_ready_projects_and_stops_removed(monkeypatch):
    from telegram_membership_saas import customer_supervisor as runtime

    pid, unready_pid = uuid4(), uuid4()
    a = _target(pid)
    b = _target(unready_pid)
    current = [a, b]
    never_ends = asyncio.Event()

    async def discover(_factory):
        return tuple(current)

    async def eligible(_factory, project_id):
        return project_id == pid

    async def run(_engine, _factory, target, *, settings):
        await never_ends.wait()

    monkeypatch.setattr(runtime, "discover_targets", discover)
    monkeypatch.setattr(runtime, "project_has_checkout", eligible)
    monkeypatch.setattr(runtime, "run_target", run)
    health = AsyncMock()
    monkeypatch.setattr(runtime, "write_bot_health", health)

    supervisor = CustomerBotSupervisor(MagicMock(), MagicMock(), _settings())
    try:
        await supervisor.reconcile_once()
        await asyncio.sleep(0)
        assert list(supervisor.workers) == [pid]
        health.assert_awaited_once()
        assert health.await_args.kwargs["state"] == "setup_needed"

        # The original Telegram bot credentials rotate: old poller is cancelled.
        current = [_target(pid, a.credential_version + timedelta(seconds=1))]
        await supervisor.reconcile_once()
        assert supervisor.workers[pid].task.cancelling()
        await asyncio.gather(
            supervisor.workers[pid].task, return_exceptions=True
        )
        await supervisor.reconcile_once()
        assert pid not in supervisor.workers
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
