"""Bounded retry policy for READ-ONLY Telegram preflight methods.

Only getMe, getWebhookInfo, getChatMember callers may use this module.
Never retry a payment, access-grant, message send or other side effect here.
Do not log Telegram exception strings: some include token-bearing API URLs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from random import uniform

from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

PREFLIGHT_ATTEMPTS = 4
RETRY_BASE_SECONDS = 0.4
RETRY_CAP_SECONDS = 3.0
MAX_RATE_LIMIT_WAIT_SECONDS = 20.0


def is_transient_telegram_error(exc: Exception) -> bool:
    """Only transport, 5xx and 429. Never retry invalid token/403/400."""
    return isinstance(
        exc, (TelegramNetworkError, TelegramServerError, TelegramRetryAfter),
    )


def preflight_retry_delay(exc: Exception, attempt: int) -> float | None:
    """Full jitter avoids synchronized 50-bot reconnect bursts.

    RetryAfter is honored only when it fits a bounded startup window.
    Longer provider limits fail closed rather than hammering Telegram.
    """
    if not is_transient_telegram_error(exc):
        return None
    if isinstance(exc, TelegramRetryAfter):
        wait = float(exc.retry_after)
        if wait > MAX_RATE_LIMIT_WAIT_SECONDS:
            return None
        return max(0.0, wait) + uniform(0.0, 0.5)
    return uniform(0.0, min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * 2 ** attempt))


async def retry_telegram_preflight[T](
    operation: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Retry one idempotent Telegram read, returning its native result.

    On exhausted retry, re-raise the original exception type for safe
    stage-specific classification by the caller. asyncio cancellation
    propagates without being swallowed or retried.
    """
    for attempt in range(PREFLIGHT_ATTEMPTS):
        try:
            return await operation()
        except Exception as exc:
            if attempt == PREFLIGHT_ATTEMPTS - 1:
                raise
            delay = preflight_retry_delay(exc, attempt)
            if delay is None:
                raise
            await sleep(delay)
    raise AssertionError("Unreachable Telegram preflight retry state")
