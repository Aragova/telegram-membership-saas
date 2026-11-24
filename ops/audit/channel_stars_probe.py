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
    for _ in range(MAX_PAGES):
        page = await fetch(offset)
        result["pages"] += 1
        for row in page.history or ():
            if not _candidate(row):
                continue
            refund = bool(getattr(row, "refund", False))
            key = (row.id, refund)
            if key in seen:
                continue
            seen.add(key)
            result["subscription_candidates"] += 1
            if getattr(row, "bot_payload", None):
                result["candidate_payload_seen"] = True
            if row.id == sale_id:
                result["sale_refund_seen" if refund else "sale_seen"] = True
            if row.id == renewal_id:
                result["renewal_refund_seen" if refund else "renewal_seen"] = True
        next_offset = page.next_offset
        if next_offset is None or next_offset == "":
            result["complete"] = True
            break
        if not isinstance(next_offset, str) or next_offset in cursors:
            break
        cursors.add(next_offset)
        offset = next_offset
    return result


async def run_live(
    api_id: int, api_hash: str, token: str, channel_id: int,
    access_hash: int, sale_id: str, renewal_id: str,
) -> dict[str, Any]:
    # Optional dependency: never installed by ViMi Pay or CI.
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.tl.functions.payments import GetStarsTransactionsRequest
    from telethon.tl.types import InputPeerChannel

    expected_bot_id = int(token.split(":", 1)[0])
    client = TelegramClient(
        MemorySession(), api_id, api_hash, receive_updates=False,
        # Telegram can redirect initial bot authorization to another DC.
        request_retries=1, connection_retries=0, flood_sleep_threshold=0,
        auto_reconnect=False,
    )
    try:
        async with asyncio.timeout(60):
            await client.connect()
            # No interactive phone-login fallback, no persisted session.
            await client.sign_in(bot_token=token)
            me = await client.get_me()
            if not me or not me.bot or me.id != expected_bot_id:
                raise ValueError("Bot identity mismatch")
            peer = InputPeerChannel(channel_id, access_hash)
            channel = await client.get_entity(peer)
            if channel.id != channel_id or not getattr(channel, "broadcast", False):
                raise ValueError("Expected a broadcast channel")

            async def fetch(offset: str) -> Any:
                return await client(
                    GetStarsTransactionsRequest(peer=peer, offset=offset, limit=PAGE_SIZE)
                )

            return await inspect_history(fetch, sale_id, renewal_id)
    finally:
        async with asyncio.timeout(10):
            await client.disconnect()


def _read_inputs() -> tuple[int, str, str, int, int, str, str]:
    if not sys.stdin.isatty():
        raise ValueError("Interactive private terminal required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        if getpass.getpass("Type ISOLATED TEST to confirm: ") != "ISOLATED TEST":
            raise ValueError("Test environment not confirmed")
        api_id = int(getpass.getpass("Test API ID: "))
        api_hash = getpass.getpass("Test API hash: ")
        token = getpass.getpass("Test bot token: ")
        channel_id = int(getpass.getpass("Test MTProto channel ID (positive): "))
        access_hash = int(getpass.getpass("Verified channel access hash: "))
        sale_id = getpass.getpass("Known test sale transaction ID: ")
        renewal_id = getpass.getpass("Known test renewal transaction ID (or blank): ")
    if api_id <= 0 or channel_id <= 0 or not api_hash or not token or not sale_id:
        raise ValueError("Missing test inputs")
    if sale_id == renewal_id:
        raise ValueError("Sale and renewal must be distinct")
    return api_id, api_hash, token, channel_id, access_hash, sale_id, renewal_id


def main() -> int:
    # Provider exceptions may contain request details. Never print them.
    logging.disable(logging.CRITICAL)
    try:
        report = asyncio.run(run_live(*_read_inputs()))
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"status": "probe_failed", "commission_authorized": False}))
        return 1
    print(json.dumps({"status": "read_ok", **report}, sort_keys=True))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
