import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.payments import PaymentRequest, create_manual_payment_method
from telegram_membership_saas.modules.plans import (
    DurationUnit,
    Plan,
    PlanResource,
    PlanStatus,
)
from telegram_membership_saas.modules.pricing import replace_active_plan_price
from telegram_membership_saas.modules.projects import Project, ProjectStatus
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.telegram_commerce import (
    create_checkout_for_telegram_user,
    list_checkout_options,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for Telegram commerce integration tests",
)


def _telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_stale_buy_button_cannot_create_request_for_suspended_project() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    now = datetime.now(UTC)

    try:
        async with session_factory() as session:
            async with session.begin():
                owner = TelegramUser(
                    telegram_user_id=_telegram_id(),
                    username=f"owner_{uuid4().hex[:8]}",
                )
                session.add(owner)
                await session.flush()
                owner_id = owner.id

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id),
        ) as session:
            project = Project(owner_user_id=owner_id, name="Suspended checkout")
            session.add(project)
            await session.flush()
            project_id = project.id

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            resource = Resource(
                project_id=project_id,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="Paid channel",
            )
            plan = Plan(
                project_id=project_id,
                name="Monthly",
                duration_value=1,
                duration_unit=DurationUnit.MONTH.value,
                status=PlanStatus.ACTIVE.value,
            )
            session.add_all([resource, plan])
            await session.flush()
            session.add(
                PlanResource(
                    project_id=project_id,
                    plan_id=plan.id,
                    resource_id=resource.id,
                )
            )
            price = await replace_active_plan_price(
                session,
                project_id=project_id,
                plan_id=plan.id,
                currency_code="USD",
                amount="10",
                changed_at=now,
            )
            method = await create_manual_payment_method(
                session,
                project_id=project_id,
                name="Card USD",
                currency_codes=["USD"],
                instruction_template="Переведите {amount} {currency} за {plan} ({period}).",
            )
            price_id = price.id
            method_id = method.id

        async with system_project_session_scope(session_factory, project_id) as session:
            options = await list_checkout_options(session, project_id=project_id)
            assert len(options) == 1

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            project = await session.get(Project, project_id)
            assert project is not None
            project.status = ProjectStatus.SUSPENDED.value
            await session.flush()

        async with system_project_session_scope(session_factory, project_id) as session:
            with pytest.raises(ValueError, match="Project is not active"):
                await create_checkout_for_telegram_user(
                    session,
                    project_id=project_id,
                    telegram_user_id=_telegram_id(),
                    username="stale_buyer",
                    first_name="Buyer",
                    plan_price_id=price_id,
                    payment_method_id=method_id,
                )
            assert await session.scalar(
                select(PaymentRequest.id).where(PaymentRequest.project_id == project_id)
            ) is None
    finally:
        await engine.dispose()
