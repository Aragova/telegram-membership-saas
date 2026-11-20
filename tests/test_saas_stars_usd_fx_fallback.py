"""Isolated rollback-only PostgreSQL checks for Stars commission USD FX.

The account invoice can be valid before its issue-day FX row is published.
Only verified CBR rates effective no later than that date and no older than
14 days may influence new Stars payment quotes. No external providers called.
"""
import os
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_saas_platform_stars_ledger import prepare

from telegram_membership_saas.config import Settings

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL with separate runtime role",
)


async def test_stars_usd_fx_missing_exact_day_uses_recent_verified_history():
    cfg = Settings()
    engine = create_async_engine(cfg.migration_database_url)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                _, _, operator, _, _ = await prepare(factory, conn)
                for day, rub in (
                    (date(2026, 1, 31), Decimal("70")),
                    (date(2026, 2, 16), Decimal("80")),
                    (date(2026, 2, 18), Decimal("82")),
                    (date(2026, 2, 20), Decimal("95")),
                ):
                    await conn.execute(text(
                        "INSERT INTO public.platform_saas_daily_fx_rates "
                        "(rate_date,currency_code,rub_per_unit,source_name,"
                        "source_date,published_by) "
                        "VALUES (:day,'USD',:rub,'CBR',:day,:operator)"
                    ), {
                        "day": day, "rub": rub, "operator": operator,
                    })
                # Mirror the inlined Stars RPC's verified issue-date rule.
                # No new SECURITY DEFINER or publicly callable resolver is
                # introduced solely for the test suite.
                def query():
                    return text(
                        "SELECT rub_per_unit FROM "
                        "public.platform_saas_daily_fx_rates "
                        "WHERE currency_code='USD' AND source_name='CBR' "
                        "AND rate_date<=:issue_day "
                        "AND rate_date>=CAST(:issue_day AS date)-interval '14 days' "
                        "AND source_date<=:issue_day "
                        "AND source_date>=CAST(:issue_day AS date)-interval '14 days' "
                        "AND source_date<=rate_date "
                        "ORDER BY rate_date DESC,source_date DESC LIMIT 1"
                    )
                assert (await conn.execute(
                    query(), {"issue_day": date(2026, 2, 19)},
                )).scalar_one() == Decimal("82")
                # No future-day rate may price an earlier invoice.
                assert (await conn.execute(
                    query(), {"issue_day": date(2026, 2, 17)},
                )).scalar_one() == Decimal("80")
                # A long gap cannot silently use a stale quotation.
                assert (await conn.execute(
                    query(), {"issue_day": date(2026, 3, 10)},
                )).scalar_one() is None
                # Direct runtime access to the FX table is blocked; the
                # owner-authorized Stars checkout RPC alone reads it.
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(
                            query(), {"issue_day": date(2026, 2, 19)},
                        )
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
