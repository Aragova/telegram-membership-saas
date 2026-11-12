"""UI-only currency labels; never change ISO/Telegram payment codes.

Keep XTR in callbacks, invoices, database, Bot API and audit records.
"""
STARS_DISPLAY_NAME = "⭐ Telegram Stars"


def display_currency(code: str) -> str:
    """Human-facing currency label in both interface languages."""
    return STARS_DISPLAY_NAME if code == "XTR" else code


def display_price(amount: str, currency: str) -> str:
    """Render an already formatted amount without changing precision."""
    return f"{amount} {display_currency(currency)}"
