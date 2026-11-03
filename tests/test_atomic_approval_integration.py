import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.entitlements import EntitlementGrant, EntitlementSource
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.outbox import OutboxEvent, OutboxEventStatus
from telegram_membership_saas.modules.payments import (
    Payment,
    PaymentRequest,
    PaymentRequestStatus,
    approve_payment_request,
    create_manual_payment_method,
    create_payment_request,
    get_or_create_project_customer,
    mark_payment_request_submitted,
)
from telegram_membership_saas.modules.plans import (
    DurationUnit,
    Plan,
    PlanResource,
    PlanStatus,
)
from telegram_membership_saas.modules.pricing import replace_active_plan_price
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import (
    Resource,
    ResourceStatus,
    ResourceType,
)
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for atomic approval integration tests",
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


async def _create_checkout_fixture(session, project_id: UUID, buyer_id: UUID):
    resource = Resource(
        project_id=project_id,
        resource_type=ResourceType.TELEGRAM_CHANNEL.value,
        external_id=f"-100{uuid4().int % 1_000_000_000_000}",
        name="Premium Channel",
        status=ResourceStatus.ACTIVE.value,
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
    )
    method = await create_manual_payment_method(
        session,
        project_id=project_id,
        name="Card USD",
        currency_codes=["USD"],
        instruction_template="Pay {amount} {currency}",
    )
    customer = await get_or_create_project_customer(
        session,
        project_id=project_id,
        user_id=buyer_id,
    )
    return resource, plan, price, method, customer


async def _create_submitted_request(
    session,
    *,
    project_id: UUID,
    customer_id: UUID,
    plan_id: UUID,
    plan_price_id: UUID,
    payment_method_id: UUID,
    submitted_at: datetime,
) -> PaymentRequest:
    request = await create_payment_request(
        session,
        project_id=project_id,
        customer_id=customer_id,
        plan_id=plan_id,
        plan_price_id=plan_price_id,
        payment_method_id=payment_method_id,
    )
    return await mark_payment_request_submitted(
        session,
        project_id=project_id,
        request_id=request.id,
        submitted_at=submitted_at,
    )


async def test_approval_is_atomic_idempotent_and_uses_frozen_duration() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        owner_id = await _create_user(session_factory, "approval_owner")
        buyer_id = await _create_user(session_factory, "approval_buyer")
        project_a = await _create_project(session_factory, owner_id, "Approval A")
        project_b = await _create_project(session_factory, owner_id, "Approval B")

        submitted_at = datetime(2027, 1, 30, 12, 0, tzinfo=UTC)
        approved_at = datetime(2027, 1, 31, 12, 0, tzinfo=UTC)

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_a),
        ) as session:
            resource, plan, price, method, customer = await _create_checkout_fixture(
                session,
                project_a,
                buyer_id,
            )
            request = await _create_submitted_request(
                session,
                project_id=project_a,
                customer_id=customer.id,
                plan_id=plan.id,
                plan_price_id=price.id,
                payment_method_id=method.id,
                submitted_at=submitted_at,
            )

            assert request.duration_value == 1
            assert request.duration_unit == DurationUnit.MONTH.value

            plan.duration_value = 1
            plan.duration_unit = DurationUnit.YEAR.value
            await session.flush()

            result = await approve_payment_request(
                session,
                project_id=project_a,
                request_id=request.id,
                reviewer_user_id=owner_id,
                reviewed_at=approved_at,
            )
            duplicate = await approve_payment_request(
                session,
                project_id=project_a,
                request_id=request.id,
                reviewer_user_id=owner_id,
                reviewed_at=approved_at,
            )

            assert result.request.status == PaymentRequestStatus.APPROVED.value
            assert duplicate.payment.id == result.payment.id
            assert result.payment.final_amount == Decimal("10.000000000000000000")
            assert result.payment.duration_value == 1
            assert result.payment.duration_unit == DurationUnit.MONTH.value
            assert result.subscription.current_period_start == approved_at
            assert result.subscription.current_period_end == datetime(
                2027,
                2,
                28,
                12,
                0,
                tzinfo=UTC,
            )
            assert len(result.grants) == 1
            assert result.grants[0].resource_id == resource.id
            assert result.grants[0].source == EntitlementSource.PAYMENT.value
            assert result.grants[0].valid_from == approved_at
            assert result.grants[0].valid_until == result.subscription.current_period_end

            payments = list(
                (
                    await session.scalars(
                        select(Payment).where(Payment.payment_request_id == request.id)
                    )
                ).all()
            )
            grants = list(
                (
                    await session.scalars(
                        select(EntitlementGrant).where(
                            EntitlementGrant.source_id == result.payment.id
                        )
                    )
                ).all()
            )
            outbox = list(
                (
                    await session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.aggregate_id == result.grants[0].id
                        )
                    )
                ).all()
            )
            assert len(payments) == 1
            assert len(grants) == 1
            assert len(outbox) == 1
            assert outbox[0].status == OutboxEventStatus.PENDING.value
            assert outbox[0].payload["customer_id"] == str(customer.id)
            assert outbox[0].payload["resource_id"] == str(resource.id)

            request_id = request.id
            payment_id = result.payment.id
            customer_id = customer.id
            plan_id = plan.id

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_b),
        ) as session:
            assert list((await session.scalars(select(Payment))).all()) == []
            assert list((await session.scalars(select(Subscription))).all()) == []
            assert list((await session.scalars(select(EntitlementGrant))).all()) == []
            assert list((await session.scalars(select(OutboxEvent))).all()) == []

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_a),
        ) as session:
            stored_request = await session.scalar(
                select(PaymentRequest).where(PaymentRequest.id == request_id)
            )
            stored_payment = await session.scalar(
                select(Payment).where(Payment.id == payment_id)
            )
            stored_subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.customer_id == customer_id,
                    Subscription.plan_id == plan_id,
                )
            )
            assert stored_request is not None
            assert stored_payment is not None
            assert stored_subscription is not None
            assert stored_request.status == PaymentRequestStatus.APPROVED.value
    finally:
        await engine.dispose()


async def test_active_renewal_extends_from_period_end() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        owner_id = await _create_user(session_factory, "renewal_owner")
        buyer_id = await _create_user(session_factory, "renewal_buyer")
        project_id = await _create_project(session_factory, owner_id, "Renewal")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            _, plan, price, method, customer = await _create_checkout_fixture(
                session,
                project_id,
                buyer_id,
            )
            first = await _create_submitted_request(
                session,
                project_id=project_id,
                customer_id=customer.id,
                plan_id=plan.id,
                plan_price_id=price.id,
                payment_method_id=method.id,
                submitted_at=datetime(2027, 1, 30, 12, 0, tzinfo=UTC),
            )
            first_result = await approve_payment_request(
                session,
                project_id=project_id,
                request_id=first.id,
                reviewer_user_id=owner_id,
                reviewed_at=datetime(2027, 1, 31, 12, 0, tzinfo=UTC),
            )

            second = await _create_submitted_request(
                session,
                project_id=project_id,
                customer_id=customer.id,
                plan_id=plan.id,
                plan_price_id=price.id,
                payment_method_id=method.id,
                submitted_at=datetime(2027, 2, 1, 12, 0, tzinfo=UTC),
            )
            second_result = await approve_payment_request(
                session,
                project_id=project_id,
                request_id=second.id,
                reviewer_user_id=owner_id,
                reviewed_at=datetime(2027, 2, 1, 12, 0, tzinfo=UTC),
            )

            assert first_result.subscription.id == second_result.subscription.id
            assert second_result.grants[0].valid_from == datetime(
                2027,
                2,
                28,
                12,
                0,
                tzinfo=UTC,
            )
            assert second_result.subscription.current_period_start == datetime(
                2027,
                2,
                28,
                12,
                0,
                tzinfo=UTC,
            )
            assert second_result.subscription.current_period_end == datetime(
                2027,
                3,
                28,
                12,
                0,
                tzinfo=UTC,
            )
    finally:
        await engine.dispose()


async def test_concurrent_duplicate_approval_creates_effects_once() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        owner_id = await _create_user(session_factory, "race_owner")
        buyer_id = await _create_user(session_factory, "race_buyer")
        project_id = await _create_project(session_factory, owner_id, "Approval Race")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            _, plan, price, method, customer = await _create_checkout_fixture(
                session,
                project_id,
                buyer_id,
            )
            request = await _create_submitted_request(
                session,
                project_id=project_id,
                customer_id=customer.id,
                plan_id=plan.id,
                plan_price_id=price.id,
                payment_method_id=method.id,
                submitted_at=datetime(2027, 4, 1, 12, 0, tzinfo=UTC),
            )
            request_id = request.id
            customer_id = customer.id
            plan_id = plan.id

        async def approve_once() -> tuple[UUID, UUID]:
            async with tenant_session_scope(
                session_factory,
                TenantContext(user_id=owner_id, project_id=project_id),
            ) as session:
                result = await approve_payment_request(
                    session,
                    project_id=project_id,
                    request_id=request_id,
                    reviewer_user_id=owner_id,
                    reviewed_at=datetime(2027, 4, 2, 12, 0, tzinfo=UTC),
                )
                return result.payment.id, result.subscription.id

        first, second = await asyncio.gather(approve_once(), approve_once())
        assert first == second

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=owner_id, project_id=project_id),
        ) as session:
            payments = list(
                (
                    await session.scalars(
                        select(Payment).where(Payment.payment_request_id == request_id)
                    )
                ).all()
            )
            subscriptions = list(
                (
                    await session.scalars(
                        select(Subscription).where(
                            Subscription.customer_id == customer_id,
                            Subscription.plan_id == plan_id,
                        )
                    )
                ).all()
            )
            assert len(payments) == 1
            assert len(subscriptions) == 1
            assert subscriptions[0].current_period_end == datetime(
                2027,
                5,
                2,
                12,
                0,
                tzinfo=UTC,
            )
    finally:
        await engine.dispose()
