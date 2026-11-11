"""Real PostgreSQL validation of immutable operator-published FX rates.

CI tests run in disposable migrated PostgreSQL; all privileged inserts are
rolled back. No production FX values, billing activation or invoices.
"""
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and restricted runtime role",
)


async def _set_actor(conn, actor):
    await conn.execute(
        text("SELECT set_config('app.current_user_id', :id, true)"),
        {"id": str(actor)},
    )


async def _publish(conn, month, currency, rate):
    is_stars = currency == "XTR"
    function = (
        "public.platform_saas_publish_fx_rate" if is_stars
        else "public.platform_saas_publish_cbr_fx_rate"
    )
    parameters = ":month,:currency,:rate" + (
        "" if is_stars else ",:source_date"
    )
    return (await conn.execute(
        text(f"SELECT {function}({parameters}) AS data").columns(data=JSONB),
        {
            "month": month, "currency": currency, "rate": rate,
            **({} if is_stars else {"source_date": month}),
        },
    )).scalar_one()


async def _snapshot(conn, month):
    return (await conn.execute(
        text(
            "SELECT public.platform_saas_monthly_fx(:month) AS data"
        ).columns(data=JSONB),
        {"month": month},
    )).scalar_one()


async def test_fx_publishing_is_owner_only_monthly_immutable_and_billing_off():
    config = Settings()
    engine = build_engine(config)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(config.migration_database_url)
    tg_owner = uuid4().int % 7000000000000000000 + 1
    tg_customer = uuid4().int % 7000000000000000000 + 1
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=tg_owner, username=None, first_name=None,
        )
        customer = await ensure_telegram_actor(
            factory, telegram_id=tg_customer, username=None, first_name=None,
        )
        month = date(2026, 12, 1)
        async with admin_engine.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(
                    text(
                        "INSERT INTO platform_operators(telegram_user_id,role) "
                        "VALUES (:telegram,'owner')"
                    ),
                    {"telegram": tg_owner},
                )
                await _set_actor(conn, owner)

                starting_invoices = (await conn.execute(text(
                    "SELECT count(*) FROM platform_saas_invoices"
                ))).scalar_one()
                empty = await _snapshot(conn, month)
                assert empty["rates"] == {}

                # Owner cannot insert an arbitrary/manual fiat rate:
                # only the authenticated official CBR publication RPC.
                with pytest.raises(DBAPIError) as refused:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_publish_fx_rate("
                            ":month,'USD',70)"
                        ), {"month": month})
                assert getattr(refused.value.orig, "sqlstate", None) == "22023"
                first = await _publish(conn, month, "USD", Decimal("70.00"))
                assert first["currency"] == "USD"
                again = await _publish(conn, month, "USD", Decimal("70"))
                assert again == first
                await _publish(conn, month, "EUR", Decimal("85.00"))
                await _publish(conn, month, "XTR", Decimal("1.04"))
                await _publish(conn, date(2027, 1, 1), "USD", Decimal("80.00"))
                assert (await _snapshot(conn, date(2027, 1, 1)))["rates"]["USD"] == 80
                stored = await _snapshot(conn, month)
                assert Decimal(str(stored["rates"]["USD"])) == Decimal("70")
                assert Decimal(str(stored["rates"]["EUR"])) == Decimal("85")
                assert len(stored["rates"]) == 3
                assert (await _snapshot(conn, date(2027, 2, 1)))["rates"] == {}

                for bad_month, currency, rate in (
                    (date(2026, 12, 2), "USD", Decimal("70")),
                    (month, "RUB", Decimal("1")),
                    (month, "usd", Decimal("70")),
                    (month, "NOTREAL", Decimal("70")),
                    (month, "UAH", Decimal("0")),
                    (month, "UAH", Decimal("-1")),
                    (month, "UAH", Decimal("0.0000000000000000001")),
                ):
                    with pytest.raises(DBAPIError):
                        async with conn.begin_nested():
                            await _publish(conn, bad_month, currency, rate)

                with pytest.raises(DBAPIError) as change:
                    async with conn.begin_nested():
                        await _publish(conn, month, "USD", Decimal("71"))
                assert getattr(change.value.orig, "sqlstate", None) == "23505"

                for operation in (
                    "UPDATE platform_saas_fx_rates SET rub_per_unit=90 "
                    "WHERE billing_month='2026-12-01' AND currency_code='USD'",
                    "DELETE FROM platform_saas_fx_rates "
                    "WHERE billing_month='2026-12-01' AND currency_code='USD'",
                ):
                    with pytest.raises(DBAPIError) as blocked:
                        async with conn.begin_nested():
                            await conn.execute(text(operation))
                    assert getattr(blocked.value.orig, "sqlstate", None) == "42501"

                # CI provisions the runtime role after Alembic migrations.
                # The production RPC grant must work with that exact role.
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                try:
                    assert (await _snapshot(conn, month))["rates"]["USD"] == 70
                finally:
                    await conn.execute(text("RESET ROLE"))

                await _set_actor(conn, customer)
                for action in (
                    lambda: _snapshot(conn, month),
                    lambda: _publish(conn, month, "UAH", Decimal("1.6")),
                ):
                    with pytest.raises(DBAPIError) as denied:
                        async with conn.begin_nested():
                            await action()
                    assert getattr(denied.value.orig, "sqlstate", None) == "42501"

                await _set_actor(conn, owner)
                flags = (await conn.execute(text(
                    "SELECT enabled FROM platform_saas_billing_control WHERE id=1"
                ))).scalar_one()
                assert flags is False
                # Shared CI database may already contain unrelated test
                # invoices. FX publication itself must never create one.
                assert (await conn.execute(text(
                    "SELECT count(*) FROM platform_saas_invoices"
                ))).scalar_one() == starting_invoices

                func = (await conn.execute(text(
                    "SELECT pg_get_functiondef("
                    "'public.platform_saas_issue_commission_invoice("
                    "uuid,uuid,date,jsonb)'::regprocedure)"
                ))).scalar_one()
                assert "public.platform_saas_daily_fx_rates" in func
                assert "Missing verified sale-day FX rate" in func
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_daily_cbr_fx_is_owner_only_immutable_and_date_specific():
    config = Settings()
    engine = build_engine(config)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(config.migration_database_url)
    owner_tg = uuid4().int % 7000000000000000000 + 1
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=owner_tg, username=None, first_name=None,
        )
        first_day, second_day = date(2026, 11, 6), date(2026, 11, 7)
        async with admin_engine.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES (:tg,'owner')"
                ), {"tg": owner_tg})
                await _set_actor(conn, owner)

                async def publish(day, rate, quoted_day=first_day):
                    return (await conn.execute(text(
                        "SELECT public.platform_saas_publish_cbr_daily_fx("
                        ":day,'USD',:rate,:quoted_day) AS data"
                    ).columns(data=JSONB), {
                        "day": day, "rate": rate, "quoted_day": quoted_day,
                    })).scalar_one()

                assert (await publish(first_day, Decimal("80")))["day"] == (
                    first_day.isoformat()
                )
                assert (await publish(first_day, Decimal("80")))["rub_per_unit"] == 80
                await publish(second_day, Decimal("90"), second_day)
                await conn.execute(text(
                    "SELECT public.platform_saas_publish_cbr_daily_fx("
                    ":day,'GBP',:rate,:source_day)"
                ), {
                    "day": first_day,
                    "rate": Decimal("107.50"),
                    "source_day": first_day,
                })
                async def snapshot(day):
                    return (await conn.execute(text(
                        "SELECT public.platform_saas_daily_fx(:day) AS data"
                    ).columns(data=JSONB), {"day": day})).scalar_one()

                assert (await snapshot(first_day))["rates"]["USD"]["rate"] == 80
                assert (await snapshot(first_day))["rates"]["GBP"]["rate"] == 107.5
                assert (await snapshot(second_day))["rates"]["USD"]["rate"] == 90

                with pytest.raises(DBAPIError) as duplicate:
                    async with conn.begin_nested():
                        await publish(first_day, Decimal("81"))
                assert getattr(duplicate.value.orig, "sqlstate", None) == "23505"

                for operation in (
                    "UPDATE platform_saas_daily_fx_rates "
                    "SET rub_per_unit=92 WHERE rate_date='2026-11-06' "
                    "AND currency_code='USD'",
                    "DELETE FROM platform_saas_daily_fx_rates "
                    "WHERE rate_date='2026-11-06' AND currency_code='USD'",
                ):
                    with pytest.raises(DBAPIError) as immutable:
                        async with conn.begin_nested():
                            await conn.execute(text(operation))
                    assert getattr(immutable.value.orig, "sqlstate", None) == "42501"

                for day, quoted_day in (
                    (first_day, date(2026, 11, 8)),
                    (first_day, date(2026, 10, 1)),
                ):
                    with pytest.raises(DBAPIError):
                        async with conn.begin_nested():
                            await publish(day, Decimal("80"), quoted_day)

                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                try:
                    assert (await snapshot(first_day))["rates"]["USD"]["rate"] == 80
                finally:
                    await conn.execute(text("RESET ROLE"))
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()
