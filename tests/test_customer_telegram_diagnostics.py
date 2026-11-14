"""Supervisor: safe, stage-specific Telegram diagnostics without token leaks."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates
import pytest

from telegram_membership_saas.customer_runtime import (
    ObservedCustomerBot,
    check_customer_bot_identity,
)
from telegram_membership_saas.customer_supervisor import safe_error_code
from telegram_membership_saas.modules.bots.diagnostics import (
    CustomerBotDiagnosticError,
    HEALTH_ERROR_CODES,
    telegram_stage_code,
)
from telegram_membership_saas.modules.platform_dashboard_handlers import (
    _ERROR_TEXT,
)
from telegram_membership_saas.modules.platform_monitor_alerts import SAFE_CODES

TOKEN = "123456:" + "A" * 35
SECRET_URL = "https://api.telegram.org/bot" + TOKEN + "/getUpdates"


def _network():
    return TelegramNetworkError(
        method=GetUpdates(timeout=1), message=SECRET_URL,
    )


@pytest.mark.parametrize(("stage", "expected"), [
    ("identity", "identity_network_failed"),
    ("webhook", "webhook_network_failed"),
    ("polling", "polling_network_failed"),
])
def test_network_errors_use_whitelisted_literals(stage, expected):
    problem = _network()
    code = telegram_stage_code(stage, problem)
    assert code == expected
    assert code in HEALTH_ERROR_CODES and code in SAFE_CODES
    assert TOKEN not in code and "api.telegram.org" not in code
    assert safe_error_code(CustomerBotDiagnosticError(code)) == code


def test_polling_conflict_is_distinct_from_network_and_stall():
    problem = TelegramConflictError(
        method=GetUpdates(timeout=1), message=SECRET_URL,
    )
    assert telegram_stage_code("polling", problem) == "polling_conflict"
    assert safe_error_code(CustomerBotDiagnosticError("polling_stalled")) == (
        "polling_stalled"
    )


def test_unknown_exception_never_leaks_sensitive_error_text():
    problem = RuntimeError(SECRET_URL)
    assert telegram_stage_code("identity", problem) == "identity_check_failed"
    assert telegram_stage_code("webhook", problem) == "webhook_check_failed"
    assert telegram_stage_code("polling", problem) == "polling_check_failed"
    assert safe_error_code(problem) == "startup_failed"
    assert TOKEN not in safe_error_code(problem)
    with pytest.raises(ValueError):
        CustomerBotDiagnosticError(SECRET_URL)


async def test_getme_network_error_carries_only_safe_stage():
    bot = MagicMock()
    bot.get_me = AsyncMock(side_effect=_network())
    bot.get_webhook_info = AsyncMock()
    with pytest.raises(CustomerBotDiagnosticError) as raised:
        await check_customer_bot_identity(bot, 123)
    assert raised.value.code == "identity_network_failed"
    assert raised.value.__cause__ is None
    bot.get_webhook_info.assert_not_awaited()


async def test_getme_unauthorized_is_invalid_credentials():
    bot = MagicMock()
    bot.get_me = AsyncMock(side_effect=TelegramUnauthorizedError(
        method=GetUpdates(timeout=1), message=SECRET_URL,
    ))
    with pytest.raises(CustomerBotDiagnosticError) as raised:
        await check_customer_bot_identity(bot, 123)
    assert raised.value.code == "invalid_credentials"


async def test_webhook_network_error_stays_distinct_from_active_webhook():
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(id=123, is_bot=True))
    bot.get_webhook_info = AsyncMock(side_effect=_network())
    with pytest.raises(CustomerBotDiagnosticError) as raised:
        await check_customer_bot_identity(bot, 123)
    assert raised.value.code == "webhook_network_failed"
    bot.delete_webhook.assert_not_called()

    bot.get_webhook_info = AsyncMock(
        return_value=SimpleNamespace(url="https://private.invalid/callback/" + TOKEN)
    )
    with pytest.raises(CustomerBotDiagnosticError) as active:
        await check_customer_bot_identity(bot, 123)
    assert active.value.code == "webhook_active"
    assert TOKEN not in str(active.value)
    bot.delete_webhook.assert_not_called()


async def test_poll_observer_reports_failure_type_then_success_without_swallowing(
    monkeypatch,
):
    response, error = [], []
    request = GetUpdates(timeout=1)
    monkeypatch.setattr(
        Bot, "__call__", AsyncMock(side_effect=[_network(), []]),
    )
    bot = ObservedCustomerBot(
        token=TOKEN, on_poll_response=lambda: response.append(True),
        on_poll_failure=error.append,
    )
    try:
        with pytest.raises(TelegramNetworkError):
            await bot(request)
        assert error == ["polling_network_failed"]
        assert response == []
        assert await bot(request) == []
        assert response == [True]
    finally:
        await bot.session.close()


def test_all_codes_have_safe_existing_monitoring_labels():
    assert HEALTH_ERROR_CODES == SAFE_CODES
    assert set(HEALTH_ERROR_CODES) - set(_ERROR_TEXT) <= {
        "lock_lost", "worker_stopped", "startup_failed",
    }
    for code, translations in _ERROR_TEXT.items():
        assert TOKEN not in code
        assert len(translations) == 2
        assert all("http" not in t and TOKEN not in t for t in translations)
