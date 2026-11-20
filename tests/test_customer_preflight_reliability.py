"""Failure-injection acceptance tests for read-only Telegram preflight reliability."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import GetUpdates

from telegram_membership_saas import customer_runtime as runtime
from telegram_membership_saas.customer_supervisor import safe_error_code
from telegram_membership_saas.modules.bots import telegram_preflight_retry as preflight
from telegram_membership_saas.modules.bots.diagnostics import CustomerBotDiagnosticError

TOKEN = "123456:" + "A" * 35
SECRET = "https://api.telegram.org/bot" + TOKEN + "/getMe"
METHOD = GetUpdates(timeout=1)


def failure(kind):
    return kind(method=METHOD, message=SECRET)


@pytest.fixture(autouse=True)
def no_backoff_wait(monkeypatch):
    """Keep tests fast without changing production retry limits."""
    monkeypatch.setattr(preflight, "uniform", lambda _a, _b: 0.1)


async def test_transient_getme_and_webhook_recover_without_process_restart():
    bot = MagicMock()
    bot.me = AsyncMock(side_effect=[
        failure(TelegramNetworkError), failure(TelegramServerError),
        SimpleNamespace(id=100, is_bot=True),
    ])
    bot.get_webhook_info = AsyncMock(side_effect=[
        failure(TelegramNetworkError), SimpleNamespace(url=""),
    ])
    # Avoid the real seconds-long backoff when exercising the full call chain.
    original = runtime.retry_telegram_preflight
    async def without_sleep(operation):
        return await original(operation, sleep=AsyncMock())
    runtime.retry_telegram_preflight = without_sleep
    try:
        await runtime.check_customer_bot_identity(bot, 100)
    finally:
        runtime.retry_telegram_preflight = original
    assert bot.me.await_count == 3
    assert bot.get_webhook_info.await_count == 2


@pytest.mark.parametrize("error,expected", [
    (TelegramBadRequest, "identity_api_failed"),
    (TelegramForbiddenError, "identity_api_failed"),
])
async def test_permanent_error_never_retries(error, expected):
    read = AsyncMock(side_effect=failure(error))
    sleeps = AsyncMock()
    with pytest.raises(error):
        await preflight.retry_telegram_preflight(read, sleep=sleeps)
    read.assert_awaited_once()
    sleeps.assert_not_awaited()


async def test_exhausted_network_failure_is_bounded_and_token_safe():
    read = AsyncMock(side_effect=failure(TelegramNetworkError))
    sleeps = AsyncMock()
    with pytest.raises(TelegramNetworkError) as err:
        await preflight.retry_telegram_preflight(read, sleep=sleeps)
    assert read.await_count == preflight.PREFLIGHT_ATTEMPTS == 4
    assert sleeps.await_count == 3
    assert sum(c.args[0] for c in sleeps.await_args_list) <= 9
    assert SECRET in str(err.value)
    # Actual emitted user-facing status must not contain exception text.
    safe = runtime.telegram_stage_code("identity", err.value)
    assert safe == "identity_network_failed"
    assert TOKEN not in safe
    assert safe_error_code(CustomerBotDiagnosticError(safe)) == safe


async def test_429_honors_bounded_retry_after_and_never_retries_huge_wait():
    minor = TelegramRetryAfter(method=METHOD, message=SECRET, retry_after=3)
    good = AsyncMock(side_effect=[minor, "ok"])
    sleeps = AsyncMock()
    assert await preflight.retry_telegram_preflight(good, sleep=sleeps) == "ok"
    assert sleeps.await_args.args[0] >= 3
    assert sleeps.await_count == 1

    huge = TelegramRetryAfter(method=METHOD, message=SECRET, retry_after=100)
    read = AsyncMock(side_effect=huge)
    sleeps.reset_mock()
    with pytest.raises(TelegramRetryAfter):
        await preflight.retry_telegram_preflight(read, sleep=sleeps)
    read.assert_awaited_once()
    sleeps.assert_not_awaited()


async def test_long_telegram_429_deadline_survives_token_safe_diagnostic():
    bot = MagicMock()
    bot.me = AsyncMock(side_effect=TelegramRetryAfter(
        method=METHOD, message=SECRET, retry_after=300,
    ))
    with pytest.raises(CustomerBotDiagnosticError) as raised:
        await runtime.check_customer_bot_identity(bot, 100)
    problem = raised.value
    assert problem.code == "identity_rate_limited"
    assert problem.retry_after == 300
    assert TOKEN not in str(problem) and SECRET not in str(problem)

    delays = AsyncMock()
    attempts = 0
    async def retry():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise problem

    await runtime.retry_customer_preflight(retry, sleep=delays)
    assert attempts == 2
    delays.assert_awaited_once()
    assert delays.await_args.args[0] >= 300


async def test_cancellation_is_not_swallowed_or_retried():
    read = AsyncMock(side_effect=asyncio.CancelledError())
    sleeps = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await preflight.retry_telegram_preflight(read, sleep=sleeps)
    read.assert_awaited_once()
    sleeps.assert_not_awaited()


def fake_resource_session(monkeypatch, resource_ids):
    session = MagicMock()
    session.scalars = AsyncMock(
        return_value=SimpleNamespace(
            all=lambda: [SimpleNamespace(external_id=rid) for rid in resource_ids],
        ),
    )

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(runtime, "system_project_session_scope", scope)


async def test_getchatmember_network_fault_then_rights_recovered(monkeypatch):
    fake_resource_session(monkeypatch, ["-10011"])
    bot = MagicMock()
    bot.id = 100
    bot.get_chat_member = AsyncMock(side_effect=[
        failure(TelegramNetworkError),
        SimpleNamespace(
            status="administrator", can_invite_users=True, can_restrict_members=True,
        ),
    ])
    original = runtime.retry_telegram_preflight
    async def without_sleep(operation):
        return await original(operation, sleep=AsyncMock())
    monkeypatch.setattr(runtime, "retry_telegram_preflight", without_sleep)
    await runtime.verify_resource_permissions(None, project_id=uuid4(), bot=bot)
    assert bot.get_chat_member.await_count == 2


async def test_exhausted_resource_network_error_never_misreports_rights(monkeypatch):
    fake_resource_session(monkeypatch, ["-10011"])
    bot = MagicMock()
    bot.id = 100
    bot.get_chat_member = AsyncMock(side_effect=failure(TelegramNetworkError))
    original = runtime.retry_telegram_preflight
    async def without_sleep(operation):
        return await original(operation, sleep=AsyncMock())
    monkeypatch.setattr(runtime, "retry_telegram_preflight", without_sleep)
    with pytest.raises(CustomerBotDiagnosticError) as caught:
        await runtime.verify_resource_permissions(None, project_id=uuid4(), bot=bot)
    assert caught.value.code == "resource_network_failed"
    assert caught.value.code != "missing_admin_rights"
    assert bot.get_chat_member.await_count == 4
    assert TOKEN not in str(caught.value)


async def test_real_missing_admin_permission_fails_closed_immediately(monkeypatch):
    fake_resource_session(monkeypatch, ["-10011", "-10012"])
    bot = MagicMock()
    bot.id = 100
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(
        status="administrator", can_invite_users=True, can_restrict_members=False,
    ))
    with pytest.raises(CustomerBotDiagnosticError) as caught:
        await runtime.verify_resource_permissions(None, project_id=uuid4(), bot=bot)
    assert caught.value.code == "missing_admin_rights"
    bot.get_chat_member.assert_awaited_once()


@pytest.mark.parametrize("error,code", [
    (TelegramForbiddenError, "resource_access_denied"),
    (TelegramBadRequest, "resource_api_failed"),
])
async def test_denied_resource_access_never_looks_like_network(monkeypatch,error,code):
    fake_resource_session(monkeypatch, ["-10011"])
    bot = MagicMock()
    bot.id = 100
    bot.get_chat_member = AsyncMock(side_effect=failure(error))
    with pytest.raises(CustomerBotDiagnosticError) as caught:
        await runtime.verify_resource_permissions(None, project_id=uuid4(), bot=bot)
    assert caught.value.code == code
    bot.get_chat_member.assert_awaited_once()


async def test_hanging_preflight_has_bounded_attempts_and_safe_network_code():
    """aiogram's 60s default can never multiply into a 240s startup."""
    async def hanging():
        await asyncio.Event().wait()

    sleeps = AsyncMock()
    with pytest.raises(TimeoutError) as caught:
        await preflight.retry_telegram_preflight(
            hanging, sleep=sleeps, timeout_seconds=0.005,
        )
    assert sleeps.await_count == preflight.PREFLIGHT_ATTEMPTS - 1
    assert runtime.telegram_stage_code("identity", caught.value) == "identity_network_failed"


async def test_identity_reports_exact_startup_stage_without_token_data(monkeypatch):
    bot = MagicMock()
    bot.me = AsyncMock(return_value=SimpleNamespace(id=100, is_bot=True))
    bot.get_webhook_info = AsyncMock(return_value=SimpleNamespace(url=""))
    phases = []

    await runtime.check_customer_bot_identity(
        bot, 100, on_startup_stage=phases.append,
    )

    assert phases == ["identity", "webhook"]
    assert all(TOKEN not in value for value in phases)
