"""Live-schema quick Stars onboarding without test buttons or lasting bots.

The entire owner/channel/plan/pricing fixture is in an outer transaction
with SAVEPOINT-backed helper sessions. A second connection verifies rollback.
"""
import os
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.owner_setup import service as setup
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires PostgreSQL transaction with clean rollback",
)


async def test_first_stars_month_plan_is_published_once_with_real_checkout():
    cfg = Settings()
    admin = create_async_engine(cfg.migration_database_url)
    try:
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                factory = async_sessionmaker(
                    conn, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                owner, _, project, _, _ = await _project(factory, _tid())
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    resource = await session.scalar(select(Resource).where(
                        Resource.project_id == project,
                    ))
                    assert resource is not None
                    resource_id = resource.id
                with pytest.raises(ValueError, match="250000"):
                    await setup.create_one_month_stars_plan(
                        factory, actor_id=owner, project_id=project,
                        resource_id=resource_id, stars_amount="1.5",
                    )
                plan_id = await setup.create_one_month_stars_plan(
                    factory, actor_id=owner, project_id=project,
                    resource_id=resource_id, stars_amount="47",
                )
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    plan = await session.get(Plan, plan_id)
                    assert (plan.duration_value, plan.duration_unit, plan.status) == (
                        1, "month", "active",
                    )
                    price = await session.scalar(select(PlanPrice).where(
                        PlanPrice.project_id == project,
                        PlanPrice.plan_id == plan_id,
                        PlanPrice.currency_code == "XTR",
                        PlanPrice.status == "active",
                    ))
                    assert price is not None
                    assert price.amount == Decimal("47")
                    assert (await session.scalar(select(PlanResource.resource_id).where(
                        PlanResource.plan_id == plan_id,
                    ))) == resource_id
                overview = await setup.setup_overview(
                    factory, actor_id=owner, project_id=project,
                )
                assert overview.sales_configured
                assert len(overview.plans) == 1
                with pytest.raises(ValueError, match="Тариф уже создан"):
                    await setup.create_one_month_stars_plan(
                        factory, actor_id=owner, project_id=project,
                        resource_id=resource_id, stars_amount="47",
                    )
                async with tenant_session_scope(
                    factory, TenantContext(user_id=owner, project_id=project),
                ) as session:
                    assert len((await session.scalars(select(Plan).where(
                        Plan.project_id == project,
                        Plan.removed_at.is_(None),
                    ))).all()) == 1
                other_owner, _, _, _, _ = await _project(factory, _tid())
                with pytest.raises((ValueError, PermissionError)):
                    await setup.create_one_month_stars_plan(
                        factory, actor_id=other_owner, project_id=project,
                        resource_id=resource_id, stars_amount="89",
                    )
            finally:
                await tx.rollback()
        async with admin.connect() as verify:
            assert (await verify.execute(text(
                "SELECT count(*) FROM projects WHERE id=:id"
            ), {"id": project})).scalar_one() == 0
    finally:
        await admin.dispose()
