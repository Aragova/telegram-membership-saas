"""Short-lived explicit receipt selection shared by file and subscription routers.

The only authorization for a receipt is still the tenant-scoped database
service. This registry merely routes the next private upload to one selected
invoice and never persists attachment bytes, file IDs or payments.
A worker restart clears the selection; buyer may tap "I have paid" again.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from uuid import UUID

_RECEIPT_TIMEOUT = 30 * 60


@dataclass(frozen=True)
class ReceiptTarget:
    kind: str
    request_id: UUID
    expires_at: float


_selected: dict[tuple[int, int], ReceiptTarget] = {}


def arm(bot_id: int, buyer_id: int, *, kind: str, request_id: UUID) -> None:
    if kind not in ("subscription", "file"):
        raise ValueError("Unknown receipt kind")
    now = monotonic()
    # Periodic lazy purge avoids long-lived stale sessions, with no worker or
    # product-count limit. Refresh is limited to selection (not every update).
    if len(_selected) > 1000:
        for key, target in list(_selected.items()):
            if target.expires_at <= now:
                _selected.pop(key, None)
    _selected[(bot_id, buyer_id)] = ReceiptTarget(
        kind=kind, request_id=request_id, expires_at=now + _RECEIPT_TIMEOUT,
    )


def peek(bot_id: int, buyer_id: int, *, kind: str) -> UUID | None:
    key = (bot_id, buyer_id)
    entry = _selected.get(key)
    if entry is None:
        return None
    if entry.expires_at <= monotonic():
        _selected.pop(key, None)
        return None
    return entry.request_id if entry.kind == kind else None


def consume(bot_id: int, buyer_id: int, *, kind: str) -> UUID | None:
    """Atomically remove one target before awaiting any Telegram/database calls."""
    target = peek(bot_id, buyer_id, kind=kind)
    if target is not None:
        _selected.pop((bot_id, buyer_id), None)
    return target


def clear(
    bot_id: int, buyer_id: int, *, kind: str, request_id: UUID,
) -> bool:
    """A stale cancel button must never clear a newer selected invoice."""
    if peek(bot_id, buyer_id, kind=kind) != request_id:
        return False
    _selected.pop((bot_id, buyer_id), None)
    return True
