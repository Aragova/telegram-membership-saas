"""Regression tests for Telegram 502/429/network outages across many bots.

No live Telegram call and no second poller. The real production worker owns
its project lease for the entire degraded/recovery cycle.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import GetUpdates

from telegram_membership_saas import customer_runtime as runtime
from telegram_membership_saas import customer_supervisor as supervisor
from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.bots.diagnostics import (
    CustomerBotDiagnosticError,
    telegram_stage_code,
)
from telegram_membership_saas.modules.bots.health_service import public_health_label
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth
from telegram_membership_saas.modules.platform_dashboard_handlers import (
    _bot_list_button_label,
    _monitoring_diagnosis,
)
from telegram_membership_saas.modules.platform_monitor_alerts import _alert_text

METHOD = GetUpdates(timeout=1)
TOKEN_URL = "https://api.telegram.org/bot123456:" + "A" * 35
_SETTINGS = Settings(telegram_central_access_enabled=False, _env_file=None)


def isolate_supervisor(
    monkeypatch, *, lease, customer, health,
    startup_timeout=None, polling_timeout=None,
):
    """One shared stubbed lease/heartbeat harness for failure injection."""
    for name, value in {
        "project_worker_lock": lease,
        "claim_bot_health": AsyncMock(),
        "confirm_project_lock": AsyncMock(),
        "run_customer_bot_instance": customer,
        "write_bot_health": health,
    }.items():
        monkeypatch.setattr(supervisor, name, value)
    monkeypatch.setattr(supervisor, "HEARTBEAT_SECONDS", 0.01)
    if startup_timeout is not None:
        monkeypatch.setattr(supervisor, "STARTUP_STALE_SECONDS", startup_timeout)
    if polling_timeout is not None:
        monkeypatch.setattr(supervisor, "POLLING_STALE_SECONDS", polling_timeout)


@pytest.mark.parametrize(("error", "code"), [
    (TelegramServerError(method=METHOD, message=TOKEN_URL), "polling_server_failed"),
    (TelegramNetworkError(method=METHOD, message=TOKEN_URL), "polling_network_failed"),
    (TelegramRetryAfter(
        method=METHOD, message=TOKEN_URL, retry_after=5,
    ), "polling_rate_limited"),
])
def test_transient_api_faults_are_token_safe_and_distinct(error, code):
    assert telegram_stage_code("polling", error) == code
    assert "api.telegram.org" not in code and "123456" not in code


@pytest.mark.parametrize("code", [
    "polling_server_failed", "polling_network_failed", "polling_rate_limited",
])
async def test_sustained_transient_outage_recovers_without_worker_restart(
    monkeypatch, code,
):
    states = []
    events = []
    recovered = asyncio.Event()
    stop = asyncio.Event()
    target = supervisor.BotTarget(uuid4(), 123456, datetime.now(UTC))

    @asynccontextmanager
    async def lease(_engine, _project):
        events.append("leased")
        try:
            yield MagicMock()
        finally:
            events.append("released")

    async def customer(
        _factory, *, project_id, telegram_bot_id, settings,
        on_poll_response, on_poll_failure, on_poll_request,
        on_startup_stage, on_preflight_failure,
    ):
        on_poll_request()
        on_poll_response()
        await asyncio.sleep(0.03)
        try:
            # More than three polling stale intervals of 502/429/timeouts.
            for _ in range(12):
                on_poll_request()
                on_poll_failure(code)
                await asyncio.sleep(0.025)
            on_poll_request()
            on_poll_response()
            recovered.set()
            await stop.wait()
        finally:
            events.append("customer_stopped")

    async def health(*_args, **kwargs):
        states.append((kwargs["state"], kwargs.get("error_code")))

    isolate_supervisor(
        monkeypatch, lease=lease, customer=customer, health=health,
        polling_timeout=0.075,
    )

    task = asyncio.create_task(supervisor.run_target(
        MagicMock(), MagicMock(), target, settings=_SETTINGS,
    ))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=2)
        await asyncio.sleep(0.03)
        assert not task.done()
        assert ("degraded", code) in states
        assert states[-1] == ("running", None)
        assert all(status != "error" for status, _ in states)
        assert events == ["leased"]
    finally:
        task.cancel()
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
    assert events[-2:] == ["customer_stopped", "released"]


async def test_repeated_safe_preflight_failures_do_not_evict_lease(monkeypatch):
    states = []
    ready = asyncio.Event()
    stop = asyncio.Event()
    target = supervisor.BotTarget(uuid4(), 123456, datetime.now(UTC))

    @asynccontextmanager
    async def lease(_engine, _project):
        yield MagicMock()

    async def customer(
        _factory, *, project_id, telegram_bot_id, settings,
        on_poll_response, on_poll_failure, on_poll_request,
        on_startup_stage, on_preflight_failure,
    ):
        on_startup_stage("identity")
        for _ in range(9):
            on_preflight_failure("identity_server_failed")
            await asyncio.sleep(0.022)
        on_startup_stage("commands")
        # The first command install occurs AFTER a long Telegram outage.
        # It must get a fresh startup deadline, not inherit the exhausted
        # getMe/preflight deadline.
        await asyncio.sleep(0.035)
        on_poll_request()
        on_poll_response()
        ready.set()
        await stop.wait()

    async def health(*_args, **kwargs):
        states.append((kwargs["state"], kwargs.get("error_code")))

    isolate_supervisor(
        monkeypatch, lease=lease, customer=customer, health=health,
        startup_timeout=0.06,
    )

    task = asyncio.create_task(supervisor.run_target(
        MagicMock(), MagicMock(), target, settings=_SETTINGS,
    ))
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        await asyncio.sleep(0.03)
        assert ("degraded", "identity_server_failed") in states
        assert states[-1] == ("running", None)
        assert not task.done()
    finally:
        task.cancel()
        stop.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_provider_cooldown_is_observed_by_startup_watchdog(monkeypatch):
    """Scaled 300s rate limit: never cancel sleep at the old 120s deadline."""
    states = []
    ready = asyncio.Event()
    stop = asyncio.Event()
    target = supervisor.BotTarget(uuid4(), 123456, datetime.now(UTC))

    @asynccontextmanager
    async def lease(_engine, _project):
        yield MagicMock()

    async def customer(
        _factory, *, project_id, telegram_bot_id, settings,
        on_poll_response, on_poll_failure, on_poll_request,
        on_startup_stage, on_preflight_failure,
    ):
        on_startup_stage("identity")
        on_preflight_failure("identity_rate_limited", 0.22)
        await asyncio.sleep(0.15)
        on_startup_stage("commands")
        await asyncio.sleep(0.02)
        on_poll_request()
        on_poll_response()
        ready.set()
        await stop.wait()

    async def health(*_args, **kwargs):
        states.append((kwargs["state"], kwargs.get("error_code")))

    isolate_supervisor(
        monkeypatch, lease=lease, customer=customer, health=health,
        startup_timeout=0.05,
    )

    task = asyncio.create_task(supervisor.run_target(
        MagicMock(), MagicMock(), target, settings=_SETTINGS,
    ))
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        await asyncio.sleep(0.025)
        assert not task.done()
        assert ("degraded", "identity_rate_limited") in states
        assert states[-1] == ("running", None)
        assert all(state != "error" for state, _ in states)
    finally:
        task.cancel()
        stop.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_outer_preflight_retries_only_transient_and_respects_cancellation():
    results = [
        CustomerBotDiagnosticError("identity_server_failed"),
        CustomerBotDiagnosticError("identity_network_failed"),
        None,
    ]
    calls = []
    notices = []
    async def read():
        calls.append(1)
        result = results.pop(0)
        if result is not None:
            raise result
    delays = []
    async def no_wait(seconds):
        delays.append(seconds)
    await runtime.retry_customer_preflight(
        read, on_failure=lambda code, pause: notices.append(code),
        sleep=no_wait,
    )
    assert len(calls) == 3
    assert notices == ["identity_server_failed", "identity_network_failed"]
    assert 0 < delays[0] < 6 and 0 < delays[1] < 8

    for code in ("invalid_credentials", "identity_mismatch", "webhook_active",
                 "missing_admin_rights", "resource_access_denied"):
        async def hard_failure(error_code=code):
            raise CustomerBotDiagnosticError(error_code)
        pauses = AsyncMock()
        with pytest.raises(CustomerBotDiagnosticError) as raised:
            await runtime.retry_customer_preflight(hard_failure, sleep=pauses)
        assert raised.value.code == code
        pauses.assert_not_awaited()


def test_degraded_health_never_claims_bot_responds():
    health = CustomerBotRuntimeHealth(
        project_id=uuid4(), telegram_bot_id=123456, instance_id=uuid4(),
        state="degraded", last_heartbeat=datetime.now(UTC),
        error_code="polling_server_failed",
    )
    title, live = public_health_label(health, telegram_bot_id=123456)
    assert title.startswith("🟡") and not live


def test_operator_sees_yellow_for_retrying_and_red_for_critical():
    moment = datetime.now(UTC)
    observation = {
        "status": "registered",
        "runtime_state": "degraded",
        "last_heartbeat": moment.isoformat(),
        "error_code": "polling_server_failed",
    }
    title, message = _monitoring_diagnosis(observation, now=moment)
    assert title.startswith("🟡")
    assert "повторяем" in message
    alert = _alert_text(
        "problem", username="testbot", status="degraded",
        error_code="polling_server_failed", heartbeat=moment,
        since=moment, timezone="Europe/Moscow",
    )
    assert "🟡" in alert and "🔴" not in alert
    assert "123456" not in alert and "api.telegram.org" not in alert
    # If Supervisor itself stopped heartbeating, an old degraded state
    # must not claim that getUpdates retries are still running.
    stale_alert = _alert_text(
        "problem", username="testbot", status="degraded",
        error_code="polling_server_failed",
        heartbeat=moment - timedelta(minutes=5), since=moment,
        timezone="Europe/Moscow", now=moment,
    )
    assert "🔴" in stale_alert and "🟡" not in stale_alert
    assert "Нет свежего сигнала Supervisor" in stale_alert
    assert _bot_list_button_label({
        **observation, "username": "demo_bot",
        "plans": 1, "subscriptions": 1, "payments_period": 0,
    }).startswith("🟡 ")
    # A stale supervisor heartbeat is NOT evidence that retry continues.
    old = {**observation, "last_heartbeat": (
        moment - timedelta(minutes=5)
    ).isoformat(), "username": "demo_bot", "plans": 1,
        "subscriptions": 1, "payments_period": 0}
    assert _bot_list_button_label(old).startswith("🟠 ")
    critical = _alert_text(
        "problem", username="testbot", status="error",
        error_code="invalid_credentials", heartbeat=moment,
        since=moment, timezone="Europe/Moscow",
    )
    assert "🔴" in critical
