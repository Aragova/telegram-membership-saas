"""Monthly official CBR FX ingestion for ViMi Pay billing, never payment activation.

The single process on supervisor shard 0 fetches one official XML quote
for the first calendar day of the Moscow month, locking immutable nominal-
normalized RUB rates for USD/EUR/UAH/BYN. Other shards never fetch.
Transient failure leaves the month unpriced and any invoice blocked.
Telegram Stars are intentionally excluded: they require a separate verified
reward-value source, not a central-bank foreign-exchange rate.
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
FIAT_CURRENCIES = ("USD", "EUR", "UAH", "BYN")


def parse_official_rates(
    payload: bytes, *, month: date,
) -> tuple[date, dict[str, Decimal]]:
    """Parse signed-host transport response, including CBR denomination.

    No default, interpolated, cached-forever or guessed rates.
    """
    if len(payload) > 256_000:
        raise ValueError("CBR response too large")
    root = ET.fromstring(payload)
    if root.tag != "ValCurs":
        raise ValueError("Unexpected central-bank response")
    quoted_on = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    if quoted_on > month or month - quoted_on > timedelta(days=14):
        raise ValueError("Official FX quote is too stale/future dated")
    rates: dict[str, Decimal] = {}
    for item in root.findall("Valute"):
        code = (item.findtext("CharCode") or "").strip()
        if code not in FIAT_CURRENCIES:
            continue
        if code in rates:
            raise ValueError("Duplicate official currency quote")
        try:
            nominal = Decimal((item.findtext("Nominal") or "").replace(",", "."))
            quoted = Decimal((item.findtext("Value") or "").replace(",", "."))
            if nominal <= 0 or quoted <= 0:
                raise ValueError("Nonpositive official FX quotation")
            rate = quoted / nominal
            if not rate.is_finite() or rate <= 0 or rate > 1000000000:
                raise ValueError("Unreasonable official FX quotation")
            rates[code] = rate.quantize(Decimal("0.000000000000000001"))
        except (InvalidOperation, ArithmeticError) as exc:
            raise ValueError("Invalid official FX quotation") from exc
    if set(rates) != set(FIAT_CURRENCIES):
        raise ValueError("Official FX data missing a required currency")
    return quoted_on, rates


async def fetch_official_month_rates(
    month: date,
) -> tuple[date, dict[str, Decimal]]:
    if month.day != 1:
        raise ValueError("Accounting month must start on day 1")
    date_req = month.strftime("%d/%m/%Y")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        follow_redirects=False,
        headers={"Accept": "application/xml"},
    ) as client:
        response = await client.get(SOURCE_URL, params={"date_req": date_req})
        response.raise_for_status()
        if response.url.host not in ("www.cbr.ru", "cbr.ru"):
            raise ValueError("Unexpected currency provider")
        return parse_official_rates(response.content, month=month)


async def sync_official_month_rates(
    factory: async_sessionmaker[AsyncSession], settings: Settings,
    *, now: datetime | None = None,
) -> bool:
    """Idempotent importer, protected by platform owner database identity.

    Called at startup / periodically on exactly one supervisor shard.
    No writes to invoice, subscription, payment or restrictions tables.
    """
    if settings.platform_owner_telegram_id is None:
        return False
    when = now or datetime.now(UTC)
    month = when.astimezone(ZoneInfo("Europe/Moscow")).date().replace(day=1)
    actor_id = await ensure_telegram_actor(
        factory, telegram_id=settings.platform_owner_telegram_id,
        username=None, first_name=None,
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        existing = (await session.execute(
            text(
                "SELECT public.platform_saas_monthly_fx(:month) AS data"
            ).columns(data=JSONB), {"month": month},
        )).scalar_one()
    missing = set(FIAT_CURRENCIES) - set(existing.get("rates", {}))
    if not missing:
        return False

    source_date, values = await fetch_official_month_rates(month)
    # A CBR rate is immutable for this month, but the DB never accepts
    # caller-provided "external" content without the operator's identity.
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        for currency in sorted(missing):
            await session.execute(text(
                "SELECT public.platform_saas_publish_cbr_fx_rate("
                ":month,:currency,:rate,:source_date)"
            ), {
                "month": month, "currency": currency,
                "rate": values[currency], "source_date": source_date,
            })
    LOG.info("ViMi Pay monthly FX published for %s", month)
    return True
