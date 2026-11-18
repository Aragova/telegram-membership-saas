"""Opaque, bounded public payment URLs for already-bound Lava gateway orders.

A shortlink does not mint a payment, and never changes the original ledger.
The UUIDv4 is already an unpredictable, immutable gateway order ID.
"""
from __future__ import annotations

import base64
import re
from uuid import UUID

SHORTLINK_BASE = "https://pay-api.usevimi.ru"
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{22}$")


def encode_lava_order(order_id: UUID) -> str:
    if not isinstance(order_id, UUID) or order_id.version != 4:
        raise ValueError("A random v4 gateway order ID is required")
    return base64.urlsafe_b64encode(order_id.bytes).rstrip(b"=").decode("ascii")


def decode_lava_order(token: str) -> UUID | None:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        return None
    try:
        result = UUID(bytes=base64.urlsafe_b64decode(token + "=="))
    except (ValueError, TypeError):
        return None
    if result.version != 4 or encode_lava_order(result) != token:
        return None
    return result


def short_lava_url(order_id: UUID) -> str:
    return SHORTLINK_BASE + "/p/" + encode_lava_order(order_id)
