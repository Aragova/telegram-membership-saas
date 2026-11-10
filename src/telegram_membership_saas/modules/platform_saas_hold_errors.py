"""Human-readable errors for DB-enforced SaaS overdue new-commerce holds."""
from functools import wraps

from sqlalchemy.exc import DBAPIError

from telegram_membership_saas.localization import tr


class CommercialHoldError(ValueError):
    """Safe, localizable, server-authored P6711 customer notice."""


def translate_commercial_hold(operation):
    """Translate ONLY the dedicated P6711 state, never mask database errors."""
    @wraps(operation)
    async def wrapped(*args, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "P6711":
                raise CommercialHoldError(tr(
                    "Новые покупки в этом боте временно недоступны из-за "
                    "неоплаченного счёта ViMi Pay. Уже оплаченный доступ "
                    "и ранее созданные заказы сохраняются. "
                    "Свяжитесь с администратором канала.",
                    "New purchases in this bot are temporarily unavailable "
                    "because of an overdue ViMi Pay invoice. Existing paid "
                    "access and earlier orders remain valid. "
                    "Contact the channel administrator.",
                )) from None
            raise
    return wrapped
