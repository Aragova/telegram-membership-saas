import os
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.plans import DurationUnit, Plan
from telegram_membership_saas.modules.pricing import (
    CurrencyAsset,
    PlanPrice,
    PlanPriceStatus,
    replace_active_plan_price,
    retire_active_plan_price,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for pricing integration tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_user(session_factory) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            user = TelegramUser(telegram_user_id=_random_telegram_id(), username="pricing_owner")
            session.add(user)
            await session.flush()
            return user.id


async def _create_project(session_factory, user_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(session_factory, TenantContext(user_id=user_id)) as session:
        project = Project(owner_user_id=user_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def _create_plan(session_factory, user_id: UUID, project_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=user_id, project_id=project_id),
    ) as session:
        plan = Plan(
            project_id=project_id,
            name=name,
            duration_value=1,
            duration_unit=DurationUnit.MONTH.value,
        )
        session.add(plan)
        await session.flush()
        return plan.id


async def test_seeded_registry_and_versioned_peer_prices_are_tenant_safe() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        user_id = await _create_user(session_factory)
        project_a = await _create_project(session_factory, user_id, "Pricing A")
        project_b = await _create_project(session_factory, user_id, "Pricing B")
        plan_a = await _create_plan(session_factory, user_id, project_a, "Monthly A")
        plan_b = await _create_plan(session_factory, user_id, project_b, "Monthly B")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_id, project_id=project_a),
        ) as session:
            codes = set((await session.scalars(select(CurrencyAsset.code))).all())
            assert {"RUB", "USD", "EUR", "USDT", "TON"} <= codes

            usd_v1 = await replace_active_plan_price(
                session,
                project_id=project_a,
                plan_id=plan_a,
                currency_code="usd",
                amount="10",
            )
            assert usd_v1.version == 1

            usd_v2 = await replace_active_plan_price(
                session,
                project_id=project_a,
                plan_id=plan_a,
                currency_code="USD",
                amount="12",
            )
            rub_v1 = await replace_active_plan_price(
                session,
                project_id=project_a,
                plan_id=plan_a,
                currency_code="RUB",
                amount="666",
            )
            assert usd_v2.version == 2
            assert rub_v1.version == 1

            with pytest.raises(ValueError, match="cannot precede"):
                await replace_active_plan_price(
                    session,
                    project_id=project_a,
                    plan_id=plan_a,
                    currency_code="USD",
                    amount="13",
                    changed_at=usd_v2.active_from - timedelta(seconds=1),
                )

            with pytest.raises(ValueError, match="cannot precede"):
                await retire_active_plan_price(
                    session,
                    project_id=project_a,
                    plan_id=plan_a,
                    currency_code="RUB",
                    retired_at=rub_v1.active_from - timedelta(seconds=1),
                )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_id, project_id=project_b),
        ) as session:
            await replace_active_plan_price(
                session,
                project_id=project_b,
                plan_id=plan_b,
                currency_code="USD",
                amount="20",
            )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_id, project_id=project_a),
        ) as session:
            prices = list(
                (
                    await session.scalars(
                        select(PlanPrice).order_by(PlanPrice.currency_code, PlanPrice.version)
                    )
                ).all()
            )
            assert {(p.currency_code, p.version) for p in prices} == {
                ("RUB", 1),
                ("USD", 1),
                ("USD", 2),
            }
            usd_history = [p for p in prices if p.currency_code == "USD"]
            assert usd_history[0].status == PlanPriceStatus.RETIRED.value
            assert usd_history[0].active_until is not None
            assert usd_history[1].status == PlanPriceStatus.ACTIVE.value
            assert usd_history[1].amount == Decimal("12.000000000000000000")

            retired = await retire_active_plan_price(
                session,
                project_id=project_a,
                plan_id=plan_a,
                currency_code="RUB",
            )
            assert retired is not None
            assert retired.status == PlanPriceStatus.RETIRED.value

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_id, project_id=project_a),
        ) as session:
            active_pairs = set(
                (
                    await session.execute(
                        select(PlanPrice.currency_code, PlanPrice.amount).where(
                            PlanPrice.status == PlanPriceStatus.ACTIVE.value
                        )
                    )
                ).all()
            )
            assert active_pairs == {("USD", Decimal("12.000000000000000000"))}
    finally:
        await engine.dispose()
