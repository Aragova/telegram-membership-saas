"""Verified official CBR FX per Moscow payment day, for dormant billing.

Only supervisor shard 0 runs this shared job. Backfill the current and
previous Moscow months so a sale on any day retains its original rate.
All output is immutable, owner-authenticated, and does not issue invoices.
XTR requires Telegram reward reconciliation, never a CBR quote.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.tenant_context import TenantContext

LOG = logging.getLogger(__name__)
SOURCE_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
CORE_FIAT = frozenset({"USD", "EUR", "UAH", "BYN"})
# Other active fiat sale currencies in migration 0004. CBR quotes these
# on the days when published; an unavailable optional code fails closed
# for its sale, never silently converts it at a made-up rate.
FIAT_CURRENCIES = CORE_FIAT | frozenset({
    "GBP", "KZT", "GEL", "AMD", "AED",
})
# Never flood the public CBR endpoint or block a supervisor reconciliation.
MAX_DAYS_PER_RUN = 7


def parse_official_rates(
    payload: bytes, *, day: date,
) -> tuple[date, dict[str, Decimal]]:
    """Normalize official CBR nominal-based rates for a requested sale day."""
    if len(payload) > 256_000:
        raise ValueError("CBR response too large")
    root = ET.fromstring(payload)
    if root.tag != "ValCurs":
        raise ValueError("Unexpected central bank response")
    actual = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    if actual > day or (day - actual).days > 14:
        raise ValueError("Official CBR rate is future-dated or too stale")
    rates: dict[str, Decimal] = {}
    for item in root.findall("Valute"):
        code = (item.findtext("CharCode") or "").strip()
        if code not in FIAT_CURRENCIES:
            continue
        if code in rates:
            raise ValueError("Duplicate currency in CBR feed")
        try:
            nominal = Decimal((item.findtext("Nominal") or "").replace(",", "."))
            value = Decimal((item.findtext("Value") or "").replace(",", "."))
            if not nominal.is_finite() or not value.is_finite():
                raise ValueError("Invalid official currency numeric value")
            if nominal <= 0 or value <= 0:
                raise ValueError("Non-positive official currency quotation")
            rate = value / nominal
            if not rate.is_finite() or not (0 < rate <= 1000000000):
                raise ValueError("Unreasonable official currency quotation")
            rates[code] = rate.quantize(Decimal("0.000000000000000001"))
        except (ArithmeticError, InvalidOperation) as exc:
            raise ValueError("Unusable official currency quotation") from exc
    if not CORE_FIAT.issubset(rates):
        raise ValueError("CBR feed is missing required currencies")
    return actual, rates


async def fetch_official_daily_rates(
    client: httpx.AsyncClient, day: date,
) -> tuple[date, dict[str, Decimal]]:
    response = await client.get(
        SOURCE_URL, params={"date_req": day.strftime("%d/%m/%Y")},
    )
    response.raise_for_status()
    if response.url.host not in ("www.cbr.ru", "cbr.ru"):
        raise ValueError("Unexpected CBR provider host")
    return parse_official_rates(response.content, day=day)


def days_to_backfill(today: date) -> list[date]:
    """Never rewrite historical quotes; cover prior billing month and today."""
    first_this_month = today.replace(day=1)
    previous_month = (first_this_month - timedelta(days=1)).replace(day=1)
    return [
        previous_month + timedelta(days=i)
        for i in range((today - previous_month).days + 1)
    ]


async def sync_official_daily_rates(
    factory: async_sessionmaker[AsyncSession], settings: Settings,
    *, now: datetime | None = None,
) -> int:
    """Fill up to seven missing CBR days without changing any historical rate.

    Returns number of days imported, so the supervisor can schedule a
    short catch-up retry. Any missing / invalid day remains unpriced.
    """
    if settings.platform_owner_telegram_id is None:
        return 0
    when = now or datetime.now(UTC)
    today = when.astimezone(ZoneInfo("Europe/Moscow")).date()
    actor_id = await ensure_telegram_actor(
        factory, telegram_id=settings.platform_owner_telegram_id,
        username=None, first_name=None,
    )
    missing: list[date] = []
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        for day in days_to_backfill(today):
            existing = (await session.execute(text(
                "SELECT public.platform_saas_daily_fx(:day) AS data"
            ).columns(data=JSONB), {"day": day})).scalar_one()
            if not CORE_FIAT.issubset(existing.get("rates", {})):
                missing.append(day)
    if not missing:
        return 0
    # UI must have today's quote first. Backfill old approval dates next.
    missing.sort(reverse=True)

    count = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0), follow_redirects=False,
        headers={"Accept": "application/xml"},
    ) as client:
        for day in missing[:MAX_DAYS_PER_RUN]:
            source_date, quotes = await fetch_official_daily_rates(client, day)
            async with tenant_session_scope(
                factory, TenantContext(user_id=actor_id),
            ) as session:
                for currency, rate in sorted(quotes.items()):
                    await session.execute(text(
                        "SELECT public.platform_saas_publish_cbr_daily_fx("
                        ":day,:currency,:rate,:source_day)"
                    ), {
                        "day": day, "currency": currency,
                        "rate": rate, "source_day": source_date,
                    })
            count += 1
    LOG.info("Official CBR daily FX synchronized: %s date(s)", count)
    return count
