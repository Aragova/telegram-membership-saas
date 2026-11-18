"""Opt-in, isolated read-only *code path* for Telegram channel Stars revenue.

IMPORTANT: MTProto USER session grants full Telegram-account capability at the
protocol layer. This service deliberately performs only two read RPCs, but it
does not make the credential itself read-only. Run only with an explicitly
authorized account and restrict the container's secret volume and network.
No login, password, 2FA, messages, payments, withdrawal or user exports here.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

CACHE_SECONDS = 45
RPC_TIMEOUT_SECONDS = 8


def _stars_decimal(value: object) -> str:
    """Latest Telegram TL StarsAmount uses amount:long and nanos:int."""
    amount = int(value.amount)
    nanos = int(getattr(value, "nanos", 0) or 0)
    if amount < 0 or not 0 <= nanos < 1_000_000_000:
        raise ValueError("Invalid Telegram revenue amount")
    precise = Decimal(amount) + Decimal(nanos) / Decimal(1_000_000_000)
    return format(precise, "f").rstrip("0").rstrip(".") if nanos else str(amount)


def normalize_telegram_revenue(
    status: object, *, chat_id: int, owner_id: int,
    now: datetime | None = None,
) -> dict[str, object]:
    """Use *only* authenticated RPC output, never member-count estimates."""
    current = _stars_decimal(status.current_balance)
    available = _stars_decimal(status.available_balance)
    withdraw = status.withdrawal_enabled
    if type(withdraw) is not bool or Decimal(available) > Decimal(current):
        raise ValueError("Invalid Telegram withdrawal state")
    next_at = getattr(status, "next_withdrawal_at", None)
    if (next_at is not None
            and (type(next_at) is not int
                 or not 0 < next_at < 4_102_444_800)):
        raise ValueError("Invalid Telegram next withdrawal timestamp")
    return {
        "chat_id": chat_id, "owner_id": owner_id,
        "current_balance": current, "available_balance": available,
        "withdrawal_enabled": withdraw,
        "next_withdrawal_at": next_at,
        "observed_at": (now or datetime.now(UTC)).isoformat(),
    }


def _configuration() -> dict[str, object]:
    """All sensitive values come from separately mounted, owner-held files."""
    api_id_raw = os.environ.get("TMS_CHANNEL_FINANCE_API_ID", "")
    owner_raw = os.environ.get("TMS_CHANNEL_FINANCE_OWNER_ID", "")
    raw_ids = os.environ.get("TMS_CHANNEL_FINANCE_ALLOWED_CHAT_IDS", "")
    try:
        api_id, owner_id = int(api_id_raw), int(owner_raw)
        chats = frozenset(int(x.strip()) for x in raw_ids.split(",") if x.strip())
    except ValueError as exc:
        raise RuntimeError("Invalid financial bridge numeric configuration") from exc
    if (api_id <= 0 or owner_id <= 0 or not chats
            or any(c >= 0 or not str(c).startswith("-100") for c in chats)):
        raise RuntimeError("Financial bridge must use explicit user and channel IDs")

    def secret_file(name: str) -> str:
        path = os.environ.get(name)
        if not path or not path.startswith("/run/secrets/"):
            raise RuntimeError("Financial bridge credentials require secret mounts")
        value = Path(path).read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError("Financial bridge secret file is empty")
        return value

    api_hash = secret_file("TMS_CHANNEL_FINANCE_API_HASH_FILE")
    session = secret_file("TMS_CHANNEL_FINANCE_SESSION_FILE")
    token = secret_file("TMS_CHANNEL_FINANCE_ACCESS_KEY_FILE")
    if len(token) < 32 or len(api_hash) < 24 or len(session) < 64:
        raise RuntimeError("Invalid financial bridge secret length")
    return {
        "api_id": api_id, "owner_id": owner_id, "allowed": chats,
        "api_hash": api_hash, "session": session, "token": token,
    }


class ChannelRevenueReader:
    """One user account / explicit channel allowlist. No mutating RPCs."""

    def __init__(self, settings: dict[str, object]):
        self.owner_id = int(settings["owner_id"])
        self.allowed = settings["allowed"]
        self.secret = str(settings["token"])
        self._settings = settings
        self._client = None
        self._lock = asyncio.Lock()
        self._cache: dict[int, tuple[float, dict[str, object]]] = {}

    async def connect(self) -> None:
        # Import only in this sidecar (never in a customer-bot worker).
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(str(self._settings["session"])),
            int(self._settings["api_id"]),
            str(self._settings["api_hash"]),
            receive_updates=False,
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram user session is no longer authorized")
            user = await client.get_me()
            if user is None or user.bot or user.id != self.owner_id:
                raise RuntimeError("MTProto session does not belong to approved owner")
        except Exception:
            await client.disconnect()
            raise
        self._client = client
        # Do not retain secret strings in our settings after connecting.
        self._settings = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    async def _telegram_read(self, chat_id: int) -> dict[str, object]:
        from telethon import functions, types, utils

        if self._client is None:
            raise RuntimeError("Reader is disconnected")
        # StringSession intentionally has no entity cache on first startup.
        # Resolve through THIS user's dialogs; never use supplied usernames.
        try:
            peer = await self._client.get_input_entity(chat_id)
        except (ValueError, TypeError):
            peer = None
            async for dialog in self._client.iter_dialogs(limit=1000):
                if utils.get_peer_id(dialog.entity) == chat_id:
                    peer = await self._client.get_input_entity(dialog.entity)
                    break
            if peer is None:
                raise PermissionError("This Telegram channel is not accessible") from None
        participant = await self._client(
            functions.channels.GetParticipantRequest(
                channel=peer, participant=types.InputPeerSelf(),
            ),
        )
        if not isinstance(participant.participant, types.ChannelParticipantCreator):
            raise PermissionError("Authorized account is not the channel owner")
        result = await self._client(
            functions.payments.GetStarsRevenueStatsRequest(
                peer=peer, dark=False, ton=False,
            ),
        )
        return normalize_telegram_revenue(
            result.status, chat_id=chat_id, owner_id=self.owner_id,
        )

    async def read(self, chat_id: int, owner_id: int) -> dict[str, object]:
        if chat_id not in self.allowed or owner_id != self.owner_id:
            raise PermissionError("Channel owner/allowlist mismatch")
        async with self._lock:
            cached = self._cache.get(chat_id)
            if cached is not None and cached[0] > time.monotonic():
                return cached[1]
            data = await asyncio.wait_for(
                self._telegram_read(chat_id), timeout=RPC_TIMEOUT_SECONDS,
            )
            self._cache[chat_id] = (time.monotonic() + CACHE_SECONDS, data)
            return data


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config = _configuration()
    reader = ChannelRevenueReader(config)
    await reader.connect()
    app.state.finance_reader = reader
    try:
        yield
    finally:
        await reader.close()


app = FastAPI(
    title="Internal Telegram Channel Revenue Reader", lifespan=_lifespan,
    docs_url=None, redoc_url=None, openapi_url=None, debug=False,
)


@app.get("/v1/channel/{chat_id}", include_in_schema=False)
async def channel_snapshot(
    chat_id: int,
    owner_id: int = Query(gt=0),
    x_vimi_finance_key: str | None = Header(default=None),
) -> JSONResponse:
    reader = app.state.finance_reader
    if (not x_vimi_finance_key
            or not secrets.compare_digest(
                x_vimi_finance_key, reader.secret,
            )):
        raise HTTPException(403, "Forbidden")
    if chat_id not in reader.allowed or owner_id != reader.owner_id:
        raise HTTPException(404, "Not available")
    try:
        snapshot = await reader.read(chat_id, owner_id)
        return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})
    except (PermissionError, TimeoutError, ValueError, RuntimeError):
        raise HTTPException(503, "Telegram revenue unavailable") from None
    except Exception:
        # Telegram exceptions can include personally identifying attributes;
        # never return or log their full string to API clients.
        raise HTTPException(503, "Telegram revenue unavailable") from None
