"""Merchant turnover: historical payment-ID deduplication and platform isolation."""

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.merchant_finances import platform_merchant_finances
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires real PostgreSQL migration and non-bypass runtime role",
)


def _tg() -> int:
    return uuid4().int % 7_000_000_000_000_000_000 + 1


async def _event(admin, project, payment, currency, amount, key):
    await admin.execute(text(
        "INSERT INTO platform_usage_events "
        "(project_id, event_type, source_id, currency_code, amount, event_key) "
        "VALUES (:project, 'payment_approved', :payment, :currency, "
        "CAST(:amount AS numeric), :key)"
    ), {
        "project": project, "payment": payment,
        "currency": currency, "amount": str(amount), "key": key,
    })


async def test_merchant_finances_original_currency_once_per_payment_after_project_delete():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner_tg, outsider_tg = _tg(), _tg()
    event_keys = []
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=owner_tg, username=None, first_name=None,
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=outsider_tg, username=None, first_name=None,
        )
        first = await create_owner_project(factory, actor_id=owner, name="First business")
        second = await create_owner_project(factory, actor_id=owner, name="Other business")
        external = await create_owner_project(
            factory, actor_id=outsider, name="Independent business",
        )
        usd_id, rub_id, other_id = uuid4(), uuid4(), uuid4()
        event_keys = [f"fin-test:{uuid4()}" for _ in range(4)]
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "INSERT INTO platform_operators(telegram_user_id, role) "
                "VALUES (:id, 'owner')"
            ), {"id": owner_tg})
            await _event(admin, first, usd_id, "USD", "12.345", event_keys[0])
            # Even duplicate ledger source IDs must not double-count turnover.
            await _event(admin, first, usd_id, "USD", "12.345", event_keys[1])
            await _event(admin, first, rub_id, "RUB", "500", event_keys[2])
            await _event(admin, external, other_id, "USD", "3.75", event_keys[3])

        # Project deletion ends a historical merchant period but does not
        # remove the original immutable usage event or its attribution.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=first),
        ) as session:
            from telegram_membership_saas.modules.projects import Project

            await session.delete(await session.get(Project, first))

        config = Settings(platform_owner_telegram_id=owner_tg)
        async with admin_engine.connect() as admin:
            merchant_id = (await admin.execute(text(
                "SELECT merchant_id FROM merchant_project_periods "
                "WHERE project_id = :project"
            ), {"project": first})).scalar_one()
            second_merchant = (await admin.execute(text(
                "SELECT merchant_id FROM merchant_project_periods "
                "WHERE project_id = :project"
            ), {"project": second})).scalar_one()
        assert merchant_id != second_merchant

        report = await platform_merchant_finances(
            factory, actor_user_id=owner, actor_telegram_id=owner_tg,
            period="all", settings=config,
        )
        # Other tests can add merchants in the same CI DB, so search all pages.
        businesses = list(report.businesses)
        for page in range(1, report.pages):
            next_page = await platform_merchant_finances(
                factory, actor_user_id=owner, actor_telegram_id=owner_tg,
                period="all", page=page, settings=config,
            )
            businesses.extend(next_page.businesses)
        archived = next(
            item for item in businesses
            if item["merchant_id"] == str(merchant_id)
        )
        assert archived["label"] == "Archived connection"
        assert archived["payments"] == 2
        currencies = {c["currency"]: c for c in archived["currencies"]}
        assert currencies["USD"]["count"] == 1
        assert Decimal(currencies["USD"]["amount"]) == Decimal("12.345")
        assert currencies["RUB"]["count"] == 1
        assert Decimal(currencies["RUB"]["amount"]) == Decimal("500")
        assert report.unmapped_payments >= 0

        # The other project of the same owner remains an independent business.
        # Only records from the original permanent merchant are included.
        assert report.merchants_total >= 2
        assert second != external

        # Spoofing the configured platform ID in the Python settings is not
        # enough: PostgreSQL must independently authorize the actual actor.
        with pytest.raises(DBAPIError):
            await platform_merchant_finances(
                factory, actor_user_id=outsider, actor_telegram_id=outsider_tg,
                period="all",
                settings=Settings(platform_owner_telegram_id=outsider_tg),
            )
        with pytest.raises(PermissionError):
            await platform_merchant_finances(
                factory, actor_user_id=outsider, actor_telegram_id=outsider_tg,
                period="all", settings=config,
            )

        async with admin_engine.connect() as admin:
            audits = (await admin.execute(text(
                "SELECT count(*) FROM platform_report_audit "
                "WHERE actor_user_id = :id AND report_kind = 'merchant_finances'"
            ), {"id": owner})).scalar_one()
            assert audits >= 1
    finally:
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "DELETE FROM platform_operators WHERE telegram_user_id = :id"
            ), {"id": owner_tg})
            if event_keys:
                await admin.execute(text(
                    "DELETE FROM platform_usage_events WHERE event_key = ANY(:keys)"
                ), {"keys": event_keys})
        await engine.dispose()
        await admin_engine.dispose()
