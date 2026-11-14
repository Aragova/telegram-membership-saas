"""Token-safe diagnostic taxonomy for the existing customer supervisor.

Never persist/log Telegram exception messages, URLs, webhook addresses,
request payloads or bot tokens. Only these fixed literals are observable.
"""
from __future__ import annotations

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)

# Keep historical values readable for already-recorded incidents.
HEALTH_ERROR_CODES = frozenset({
    "startup_failed", "invalid_credentials", "telegram_check_failed",
    "missing_admin_rights", "worker_stopped", "lock_lost",
    "identity_mismatch", "identity_network_failed",
    "identity_api_failed", "identity_check_failed",
    "webhook_active", "webhook_network_failed",
    "webhook_api_failed", "webhook_check_failed",
    "polling_network_failed", "polling_api_failed",
    "polling_conflict", "polling_stalled", "polling_check_failed",
})


class CustomerBotDiagnosticError(RuntimeError):
    """Explicit failure stage + safe code, without retaining an exception."""

    def __init__(self, code: str) -> None:
        if code not in HEALTH_ERROR_CODES:
            raise ValueError("Unsupported customer-bot diagnostic code")
        self.code = code
        super().__init__(code)


def telegram_stage_code(stage: str, exc: Exception) -> str:
    """Classify by exception TYPE only; no untrusted error text is inspected.

    getUpdates errors can be swallowed and retried internally by aiogram;
    the supervisor receives this fixed code from its observer separately.
    """
    if stage not in {"identity", "webhook", "polling"}:
        raise ValueError("Unsupported Telegram diagnostic stage")
    if isinstance(exc, TelegramUnauthorizedError):
        return "invalid_credentials"
    if stage == "polling" and isinstance(exc, TelegramConflictError):
        return "polling_conflict"
    if isinstance(exc, (TelegramNetworkError, OSError, TimeoutError)):
        return f"{stage}_network_failed"
    if isinstance(exc, TelegramAPIError):
        return f"{stage}_api_failed"
    return f"{stage}_check_failed"
