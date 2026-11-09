"""Native Telegram date-time entities for subscription and payment UI.

This is presentation only: never modify the UTC values used for billing or
access. The readable inner text is shown by clients without native rendering.
Official Bot API HTML: <tg-time unix="..." format="dt|d|r">fallback</tg-time>.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

_ALLOWED_FORMATS = frozenset({"d", "dt", "r"})
_MAX_FUTURE = timedelta(days=1098)


def native_telegram_time(
    value: datetime,
    timezone: str,
    *,
    fmt: str = "dt",
    now: datetime | None = None,
) -> str:
    """Return trusted HTML with a readable project-local fallback.

    Telegram limits dates to [Unix epoch, now + 1098 days]. Use plain fallback
    outside that range instead of emitting an entity rejected by Telegram.
    Naive datetimes are rejected so the server's local timezone never changes
    a subscription deadline by accident.
    """
    if fmt not in _ALLOWED_FORMATS:
        raise ValueError("Unsupported Telegram date-time format")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Subscription timestamps must be timezone-aware")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Comparison time must be timezone-aware")
    fallback = value.astimezone(ZoneInfo(timezone)).strftime(
        "%d.%m.%Y" if fmt == "d" else "%d.%m.%Y %H:%M"
    )
    timestamp = int(value.timestamp())
    if timestamp < 0 or value > current + _MAX_FUTURE:
        return escape(fallback)
    return (
        f'<tg-time unix="{timestamp}" format="{fmt}">'
        + escape(fallback) + "</tg-time>"
    )
