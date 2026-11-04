from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.outbox import (
    OutboxEvent,
    OutboxEventStatus,
    OutboxEventType,
)
from telegram_membership_saas.modules.payments.models import (
    Payment,
    PaymentMethod,
    PaymentRequest,
    PaymentRequestStatus,
)
from telegram_membership_saas.modules.payments.request_service import (
    load_payment_request_for_update,
    require_aware_timestamp,
    require_payment_permission,
    utcnow,
)
from telegram_membership_saas.modules.plans import PlanResource
from telegram_membership_saas.modules.projects import Permission
from telegram_membership_saas.modules.subscriptions import (
    Subscription,
    SubscriptionStatus,
    calculate_period_end,
)
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    request: PaymentRequest
    payment: Payment
    subscription: Subscription
    grants: tuple[EntitlementGrant, ...]


async def _load_existing_result(
    session: AsyncSession,
    *,
    request: PaymentRequest,
) -> ApprovalResult:
    payment = await session.scalar(
        select(Payment).where(
            Payment.project_id == request.project_id,
            Payment.payment_request_id == request.id,
        )
    )
    if payment is None:
        raise RuntimeError("Approved payment request has no Payment")

    subscription = await session.scalar(
        select(Subscription).where(
            Subscription.project_id == request.project_id,
            Subscription.customer_id == request.customer_id,
            Subscription.plan_id == request.plan_id,
        )
    )
    if subscription is None:
        raise RuntimeError("Approved payment request has no Subscription")

    grants = tuple(
        (
            await session.scalars(
                select(EntitlementGrant)
                .where(
                    EntitlementGrant.project_id == request.project_id,
                    EntitlementGrant.source == EntitlementSource.PAYMENT.value,
                    EntitlementGrant.source_id == payment.id,
                )
                .order_by(EntitlementGrant.id)
            )
        ).all()
    )
    return ApprovalResult(
        request=request,
        payment=payment,
        subscription=subscription,
        grants=grants,
    )


async def _lock_customer(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
) -> ProjectCustomer:
    customer = await session.scalar(
        select(ProjectCustomer)
        .where(
            ProjectCustomer.id == customer_id,
            ProjectCustomer.project_id == project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if customer is None:
        raise RuntimeError("Payment request customer disappeared")
    return customer


async def _load_subscription_for_update(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    plan_id: UUID,
) -> Subscription | None:
    return await session.scalar(
        select(Subscription)
        .where(
            Subscription.project_id == project_id,
            Subscription.customer_id == customer_id,
            Subscription.plan_id == plan_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )


async def approve_payment_request(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
    reviewer_user_id: UUID,
    reviewed_at: datetime | None = None,
) -> ApprovalResult:
    # Serialize paid grants with owner retirement and early subscription end.
    await checkout_retirement_lock(session, project_id)
    project = await require_payment_permission(
        session,
        project_id=project_id,
        actor_user_id=reviewer_user_id,
        permission=Permission.PAYMENT_APPROVE,
    )
    request = await load_payment_request_for_update(
        session,
        project_id=project_id,
        request_id=request_id,
    )

    if request.status == PaymentRequestStatus.APPROVED.value:
        return await _load_existing_result(session, request=request)
    if request.status != PaymentRequestStatus.WAITING_REVIEW.value:
        raise ValueError("Only a request waiting for review can be approved")

    moment = reviewed_at or utcnow()
    require_aware_timestamp(moment)
    if request.submitted_at is None or moment < request.submitted_at:
        raise ValueError("reviewed_at cannot precede submission")

    await _lock_customer(
        session,
        project_id=project_id,
        customer_id=request.customer_id,
    )

    provider = await session.scalar(
        select(PaymentMethod.provider).where(
            PaymentMethod.id == request.payment_method_id,
            PaymentMethod.project_id == project_id,
        )
    )
    if provider is None:
        raise RuntimeError("Payment method disappeared before approval")

    payment = Payment(
        project_id=project_id,
        payment_request_id=request.id,
        customer_id=request.customer_id,
        plan_id=request.plan_id,
        plan_price_id=request.plan_price_id,
        payment_method_id=request.payment_method_id,
        provider=provider,
        currency_code=request.currency_code,
        regular_amount=request.regular_amount,
        discount_amount=request.discount_amount,
        final_amount=request.final_amount,
        promotion_id=request.promotion_id,
        duration_value=request.duration_value,
        duration_unit=request.duration_unit,
        approved_at=moment,
        approved_by=reviewer_user_id,
    )
    session.add(payment)
    await session.flush()

    subscription = await _load_subscription_for_update(
        session,
        project_id=project_id,
        customer_id=request.customer_id,
        plan_id=request.plan_id,
    )

    if subscription is not None and subscription.current_period_end is None:
        grant_start = moment
        grant_end = calculate_period_end(
            grant_start,
            timezone_name=project.timezone,
            duration_value=request.duration_value,
            duration_unit=request.duration_unit,
        )
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.last_payment_id = payment.id
    else:
        previous_end = subscription.current_period_end if subscription is not None else None
        grant_start = previous_end if previous_end is not None and previous_end > moment else moment
        grant_end = calculate_period_end(
            grant_start,
            timezone_name=project.timezone,
            duration_value=request.duration_value,
            duration_unit=request.duration_unit,
        )

        if subscription is None:
            subscription = Subscription(
                project_id=project_id,
                customer_id=request.customer_id,
                plan_id=request.plan_id,
                status=SubscriptionStatus.ACTIVE.value,
                started_at=grant_start,
                current_period_start=grant_start,
                current_period_end=grant_end,
                last_payment_id=payment.id,
            )
            session.add(subscription)
        else:
            subscription.status = SubscriptionStatus.ACTIVE.value
            subscription.current_period_start = grant_start
            subscription.current_period_end = grant_end
            subscription.last_payment_id = payment.id

    resource_ids = tuple(
        (
            await session.scalars(
                select(PlanResource.resource_id)
                .where(
                    PlanResource.project_id == project_id,
                    PlanResource.plan_id == request.plan_id,
                )
                .order_by(PlanResource.resource_id)
            )
        ).all()
    )

    grants = tuple(
        EntitlementGrant(
            project_id=project_id,
            customer_id=request.customer_id,
            resource_id=resource_id,
            source=EntitlementSource.PAYMENT.value,
            source_id=payment.id,
            status=EntitlementGrantStatus.ACTIVE.value,
            valid_from=grant_start,
            valid_until=grant_end,
        )
        for resource_id in resource_ids
    )
    session.add_all(grants)
    await session.flush()

    outbox_events = [
        OutboxEvent(
            project_id=project_id,
            event_type=OutboxEventType.ACCESS_RECALCULATE.value,
            aggregate_type="entitlement_grant",
            aggregate_id=grant.id,
            dedupe_key=f"entitlement_grant:{grant.id}:access_recalculate",
            payload={
                "customer_id": str(request.customer_id),
                "resource_id": str(grant.resource_id),
                "grant_id": str(grant.id),
            },
            status=OutboxEventStatus.PENDING.value,
        )
        for grant in grants
    ]
    session.add_all(outbox_events)

    request.status = PaymentRequestStatus.APPROVED.value
    request.reviewed_at = moment
    request.reviewed_by = reviewer_user_id

    await session.flush()
    return ApprovalResult(
        request=request,
        payment=payment,
        subscription=subscription,
        grants=grants,
    )
