import os
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.payments import (
    PaymentRequest,
    PaymentRequestStatus,
    create_manual_payment_method,
    create_payment_request,
    get_or_create_project_customer,
    mark_payment_request_submitted,
    reject_payment_request,
)
from telegram_membership_saas.modules.plans import DurationUnit, Plan, PlanStatus
from telegram_membership_saas.modules.pricing import replace_active_plan_price
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for payment request integration tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_user(session_factory, username: str) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            user = TelegramUser(telegram_user_id=_random_telegram_id(), username=username)
            session.add(user)
            await session.flush()
            return user.id


async def _create_project(session_factory, owner_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(session_factory, TenantContext(user_id=owner_id)) as session:
        project = Project(owner_user_id=owner_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def _create_active_plan(session, project_id: UUID, name: str) -> Plan:
    plan = Plan(
        project_id=project_id,
        name=name,
        duration_value=1,
        duration_unit=DurationUnit.MONTH.value,
        status=PlanStatus.ACTIVE.value,
    )
    session.add(plan)
    await session.flush()
    return plan


async def test_payment_request_snapshots_price_and_is_tenant_safe() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        owner_id = await _create_user(session_factory, "request_owner")
        buyer_id = await _create_user(session_factory, "request_buyer")
        project_a = await _create_project(session_factory, owner_id, "Requests A")
        project_b = await _create_project(session_factory, owner_id, "Requests B")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_a),
        ) as session:
            plan = await _create_active_plan(session, project_a, "Monthly")
            price_v1 = await replace_active_plan_price(
                session,
                project_id=project_a,
                plan_id=plan.id,
                currency_code="USD",
                amount="10",
            )
            method = await create_manual_payment_method(
                session,
                project_id=project_a,
                name="Card USD",
                currency_codes=["USD"],
                instruction_template="Pay {amount} {currency} for {plan} / {period}",
            )
            customer = await get_or_create_project_customer(
                session,
                project_id=project_a,
                user_id=buyer_id,
            )
            same_customer = await get_or_create_project_customer(
                session,
                project_id=project_a,
                user_id=buyer_id,
            )
            assert same_customer.id == customer.id

            request = await create_payment_request(
                session,
                project_id=project_a,
                customer_id=customer.id,
                plan_id=plan.id,
                plan_price_id=price_v1.id,
                payment_method_id=method.id,
            )
            assert request.status == PaymentRequestStatus.WAITING_CONFIRMATION.value
            assert request.regular_amount == Decimal("10.000000000000000000")
            assert request.final_amount == Decimal("10.000000000000000000")
            assert request.discount_amount == Decimal("0")
            request_id = request.id
            customer_id = customer.id

            price_v2 = await replace_active_plan_price(
                session,
                project_id=project_a,
                plan_id=plan.id,
                currency_code="USD",
                amount="12",
            )
            assert price_v2.version == 2
            assert request.regular_amount == Decimal("10.000000000000000000")

            submitted = await mark_payment_request_submitted(
                session,
                project_id=project_a,
                request_id=request_id,
            )
            duplicate_submit = await mark_payment_request_submitted(
                session,
                project_id=project_a,
                request_id=request_id,
            )
            assert duplicate_submit.id == submitted.id
            assert submitted.status == PaymentRequestStatus.WAITING_REVIEW.value
            assert submitted.submitted_at is not None

            rejected = await reject_payment_request(
                session,
                project_id=project_a,
                request_id=request_id,
                reviewer_user_id=owner_id,
            )
            duplicate_reject = await reject_payment_request(
                session,
                project_id=project_a,
                request_id=request_id,
                reviewer_user_id=owner_id,
            )
            assert duplicate_reject.id == rejected.id
            assert rejected.status == PaymentRequestStatus.REJECTED.value
            assert rejected.reviewed_by == owner_id
            assert rejected.reviewed_at is not None

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_a),
        ) as session:
            stored = await session.scalar(
                select(PaymentRequest).where(PaymentRequest.id == request_id)
            )
            assert stored is not None
            assert stored.regular_amount == Decimal("10.000000000000000000")
            assert stored.final_amount == Decimal("10.000000000000000000")
            assert stored.status == PaymentRequestStatus.REJECTED.value

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_b),
        ) as session:
            assert list((await session.scalars(select(PaymentRequest))).all()) == []
            assert list((await session.scalars(select(ProjectCustomer))).all()) == []

            with pytest.raises(ValueError, match="Customer is not accessible"):
                await create_payment_request(
                    session,
                    project_id=project_b,
                    customer_id=customer_id,
                    plan_id=uuid4(),
                    plan_price_id=uuid4(),
                    payment_method_id=uuid4(),
                )
    finally:
        await engine.dispose()
