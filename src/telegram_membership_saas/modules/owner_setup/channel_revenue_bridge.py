"""Client for an opt-in, isolated Telegram USER-session revenue reader.

The bot supervisor never receives a Telegram user session, app hash, 2FA
password or sign-in code. No channel balance is inferred from bot revenue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from telegram_membership_saas.config import get_settings

MAX_SNAPSHOT_AGE = timedelta(seconds=120)
REQUEST_TIMEOUT = 3.0


@dataclass(frozen=True, slots=True)
class ChannelRevenue:
    current_balance: Decimal
    available_balance: Decimal
    withdrawal_enabled: bool
    next_withdrawal_at: datetime | None
    observed_at: datetime


def _read_decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Amount is not a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid Telegram Stars amount") from exc
    if (not amount.is_finite() or amount < 0
            or amount.as_tuple().exponent < -9):
        raise ValueError("Invalid nonnegative nanostar balance")
    return amount


def parse_channel_revenue(
    raw: object, *, chat_id: int, owner_id: int, now: datetime | None = None,
) -> ChannelRevenue:
    """Reject responses for the wrong channel, account, or stale snapshots."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid revenue response")
    if (type(raw.get("chat_id")) is not int or raw["chat_id"] != chat_id
            or type(raw.get("owner_id")) is not int
            or raw["owner_id"] != owner_id):
        raise ValueError("Channel or verified owner does not match")
    if type(raw.get("withdrawal_enabled")) is not bool:
        raise ValueError("Invalid Telegram withdrawal flag")
    current = _read_decimal(raw.get("current_balance"))
    available = _read_decimal(raw.get("available_balance"))
    if available > current:
        raise ValueError("Available balance exceeds current balance")
    stamp = raw.get("observed_at")
    if not isinstance(stamp, str):
        raise ValueError("Missing Telegram observation timestamp")
    try:
        observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid observation timestamp") from exc
    moment = now or datetime.now(UTC)
    if (observed.tzinfo is None or observed > moment + timedelta(seconds=10)
            or moment - observed > MAX_SNAPSHOT_AGE):
        raise ValueError("Stale Telegram revenue observation")
    next_timestamp = raw.get("next_withdrawal_at")
    if next_timestamp is None:
        upcoming = None
    elif (type(next_timestamp) is int
          and 0 < next_timestamp < 4_102_444_800):
        upcoming = datetime.fromtimestamp(next_timestamp, tz=UTC)
    else:
        raise ValueError("Invalid Telegram withdrawal unlock timestamp")
    return ChannelRevenue(
        current_balance=current, available_balance=available,
        withdrawal_enabled=raw["withdrawal_enabled"],
        next_withdrawal_at=upcoming, observed_at=observed,
    )


def _checked_url(raw: str) -> str:
    # This is operator configuration, but do not ever forward bot credentials
    # to a public host or an arbitrary URL if a deployment is misconfigured.
    url = urlsplit(raw.rstrip("/"))
    if (url.scheme != "http" or url.hostname not in
            {"channel-finance", "127.0.0.1", "localhost"}
            or not url.port or url.username or url.password
            or url.query or url.fragment or url.path not in ("", "/")):
        raise ValueError("Finance reader must use the private internal service")
    return raw.rstrip("/")


async def live_channel_revenue(
    *, chat_id: int, owner_id: int,
    client: httpx.AsyncClient | None = None,
) -> ChannelRevenue | None:
    """Failure means unknown, NEVER zero or 'withdrawal enabled'.

    Only the isolated MTProto reader can hold a user session. Bot workers
    receive a short-lived, owner-bound snapshot over a private service URL.
    """
    settings = get_settings()
    if (not settings.channel_finance_bridge_url
            or not settings.channel_finance_bridge_token_file
            or chat_id >= 0 or owner_id <= 0):
        return None
    try:
        url = _checked_url(settings.channel_finance_bridge_url)
        token = Path(settings.channel_finance_bridge_token_file).read_text(
            encoding="ascii",
        ).strip()
        if len(token) < 32:
            return None
        own_client = client is None
        transport = client or httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, trust_env=False, follow_redirects=False,
        )
        try:
            response = await transport.get(
                f"{url}/v1/channel/{chat_id}",
                params={"owner_id": owner_id},
                headers={"x-vimi-finance-key": token},
            )
            response.raise_for_status()
            if len(response.content) > 4096:
                return None
            return parse_channel_revenue(
                response.json(), chat_id=chat_id, owner_id=owner_id,
            )
        finally:
            if own_client:
                await transport.aclose()
    except (OSError, ValueError, httpx.HTTPError):
        return None
