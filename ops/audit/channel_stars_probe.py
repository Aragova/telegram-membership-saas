"""Isolated operator-only MTProto experiment; never a billing source."""

import asyncio
import getpass
import json
import logging
import sys
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

MAX_PAGES = 20
PAGE_SIZE = 50
PERIOD = 30 * 24 * 60 * 60


class ProbeValidationError(ValueError):
    """Invalid local inputs or mismatched provider identity."""


def _candidate(row: Any) -> bool:
    """A protocol hint, not proof of a ViMi purchase or its attribution."""
    return (
        getattr(row, "subscription_period", None) == PERIOD
        and not getattr(row, "pending", False)
        and not getattr(row, "failed", False)
    )


async def inspect_history(
    fetch: Callable[[str], Awaitable[Any]], sale_id: str, renewal_id: str
) -> dict[str, Any]:
    """Report only aggregate evidence; keep IDs and cursors in memory."""
    result = {
        "pages": 0,
        "complete": False,
        "subscription_candidates": 0,
        "sale_seen": False,
        "renewal_seen": False,
        "sale_refund_seen": False,
        "renewal_refund_seen": False,
        "candidate_payload_seen": False,
        "commission_authorized": False,
    }
    offset = ""
    cursors = {offset}
    seen: set[tuple[str, bool]] = set()
    missing = object()
    for _ in range(MAX_PAGES):
        page = await fetch(offset)
        # Official payments.starsStatus TL schema: history, not transactions.
        # https://core.telegram.org/constructor/payments.starsStatus
        history = getattr(page, "history", missing)
        next_offset = getattr(page, "next_offset", missing)
        # Missing fields are not an authoritative empty history or final page.
        if history is missing or next_offset is missing:
            break
        if history is not None and not isinstance(history, (list, tuple)):
            break
        result["pages"] += 1
        for row in history or ():
            if not _candidate(row):
                continue
            refund = bool(getattr(row, "refund", False))
            row_id = getattr(row, "id", None)
            if not isinstance(row_id, str) or not row_id:
                return result  # Incomplete; never silently discard malformed evidence.
            key = (row_id, refund)
            if key in seen:
                continue
            seen.add(key)
            result["subscription_candidates"] += 1
            if getattr(row, "bot_payload", None):
                result["candidate_payload_seen"] = True
            if row_id == sale_id:
                result["sale_refund_seen" if refund else "sale_seen"] = True
            if renewal_id and row_id == renewal_id:
                result["renewal_refund_seen" if refund else "renewal_seen"] = True
        if next_offset is None or next_offset == "":
            result["complete"] = True
            break
        if not isinstance(next_offset, str) or next_offset in cursors:
            break
        cursors.add(next_offset)
        offset = next_offset
    return result


def _bot_id(token: str) -> int:
    prefix, separator, secret = token.partition(":")
    if not separator or not secret or not prefix.isascii() or not prefix.isdigit():
        raise ProbeValidationError("Invalid bot token format")
    bot_id = int(prefix)
    if bot_id <= 0:
        raise ProbeValidationError("Invalid bot token format")
    return bot_id


async def run_live(
    api_id: int, api_hash: str, token: str, channel_id: int,
    access_hash: int, sale_id: str, renewal_id: str,
) -> dict[str, Any]:
    # Optional dependency: never installed by ViMi Pay or CI.
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.tl.functions.payments import GetStarsTransactionsRequest
    from telethon.tl.types import InputPeerChannel

    expected_bot_id = _bot_id(token)
    client = TelegramClient(
        MemorySession(), api_id, api_hash, receive_updates=False,
        # Telegram can redirect initial bot authorization to another DC.
        request_retries=1, connection_retries=0, flood_sleep_threshold=0,
        auto_reconnect=False,
    )
    primary_failed = False
    try:
        async with asyncio.timeout(60):
            await client.connect()
            # No interactive phone-login fallback, no persisted session.
            await client.sign_in(bot_token=token)
            me = await client.get_me()
            if getattr(me, "bot", False) is not True or getattr(me, "id", None) != expected_bot_id:
                raise ProbeValidationError("Bot identity mismatch")
            peer = InputPeerChannel(channel_id, access_hash)
            channel = await client.get_entity(peer)
            if (getattr(channel, "id", None) != channel_id
                    or getattr(channel, "broadcast", False) is not True):
                raise ProbeValidationError("Expected a broadcast channel")

            async def fetch(offset: str) -> Any:
                return await client(
                    GetStarsTransactionsRequest(peer=peer, offset=offset, limit=PAGE_SIZE)
                )

            return await inspect_history(fetch, sale_id, renewal_id)
    except BaseException:
        primary_failed = True
        raise
    finally:
        try:
            async with asyncio.timeout(10):
                await client.disconnect()
        except Exception:
            # Preserve the primary failure; a cleanup-only failure still fails closed.
            if not primary_failed:
                raise


def _read_integer(prompt: str) -> int:
    try:
        return int(getpass.getpass(prompt))
    except ValueError:
        raise ProbeValidationError("Invalid integer input") from None


def _read_inputs() -> tuple[int, str, str, int, int, str, str]:
    if not sys.stdin.isatty():
        raise ProbeValidationError("Interactive private terminal required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        if getpass.getpass("Type ISOLATED TEST to confirm: ") != "ISOLATED TEST":
            raise ProbeValidationError("Test environment not confirmed")
        api_id = _read_integer("Test API ID: ")
        api_hash = getpass.getpass("Test API hash: ")
        token = getpass.getpass("Test bot token: ")
        channel_id = _read_integer("Test MTProto channel ID (positive): ")
        access_hash = _read_integer("Verified channel access hash: ")
        sale_id = getpass.getpass("Known test sale transaction ID: ")
        renewal_id = getpass.getpass("Known test renewal transaction ID (or blank): ")
    if api_id <= 0 or channel_id <= 0 or not api_hash or not token or not sale_id:
        raise ProbeValidationError("Missing test inputs")
    _bot_id(token)
    if sale_id == renewal_id:
        raise ProbeValidationError("Sale and renewal must be distinct")
    return api_id, api_hash, token, channel_id, access_hash, sale_id, renewal_id


def _failure_reason(exc: BaseException) -> str:
    """Return only a fixed allowlisted code, never provider exception text."""
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    if isinstance(exc, ProbeValidationError):
        return "invalid_input_or_identity"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ImportError):
        return "dependency_unavailable"
    if isinstance(exc, OSError):
        return "connection_error"
    if type(exc).__name__ in {"ChatAdminRequiredError", "BotAccessForbiddenError",
                             "ChannelPrivateError", "PeerIdInvalidError"}:
        return "peer_access_denied"
    if type(exc).__name__ == "FloodWaitError":
        return "rate_limited"
    return "provider_or_runtime_error"


def main() -> int:
    # Provider exceptions may contain request details. Never print them.
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run_live(*_read_inputs()))
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({
            "status": "probe_failed", "commission_authorized": False,
            "reason": _failure_reason(exc),
        }))
        return 1
    print(json.dumps({"status": "read_ok", **report}, sort_keys=True))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
