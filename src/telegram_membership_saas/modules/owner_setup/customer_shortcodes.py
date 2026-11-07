"""Compact Telegram bot commands for opening a project-scoped customer card.

They are navigational references, NOT access tokens. Each command resolves
against the actual connected bot's project under tenant RLS and an admin role.
"""
from __future__ import annotations

import base64
import re
from uuid import UUID

_SHORTCODE = re.compile(r"^/(?:u[0-9a-z]{1,13}|c[a-z2-7]{26})$")
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
_MAX_TELEGRAM_ID = 2**63 - 1


def customer_command(*, telegram_id: int | None, customer_id: UUID) -> str:
    """Use a short base36 Telegram ID; legacy queued alerts can use UUID."""
    if telegram_id is None:
        return "/c" + base64.b32encode(customer_id.bytes).decode("ascii").lower().rstrip("=")
    if not isinstance(telegram_id, int) or not 0 < telegram_id <= _MAX_TELEGRAM_ID:
        raise ValueError("Invalid Telegram ID")
    digits = []
    number = telegram_id
    while number:
        number, rem = divmod(number, 36)
        digits.append(_DIGITS[rem])
    return "/u" + "".join(reversed(digits))


def parse_customer_command(text: str) -> tuple[int | None, UUID | None]:
    """Decode canonical bot command without trusting it for authorization."""
    if not _SHORTCODE.fullmatch(text):
        raise ValueError("Invalid customer command")
    if text.startswith("/u"):
        number = int(text[2:], 36)
        if number > _MAX_TELEGRAM_ID or customer_command(
            telegram_id=number, customer_id=UUID(int=0),
        ) != text:
            raise ValueError("Invalid customer command")
        return number, None
    try:
        raw = base64.b32decode(text[2:].upper() + "======")
        customer_id = UUID(bytes=raw)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("Invalid customer command") from exc
    if customer_command(telegram_id=None, customer_id=customer_id) != text:
        raise ValueError("Invalid customer command")
    return None, customer_id
