from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.payments.models import (
    PaymentMethod,
    PaymentMethodCurrency,
    PaymentMethodStatus,
    PaymentRequest,
    PaymentRequestStatus,
)
from telegram_membership_saas.modules.plans import Plan, PlanStatus
from telegram_membership_saas.modules.pricing import (
    CurrencyAsset,
    CurrencyAssetStatus,
    PlanPrice,
    PlanPriceStatus,
)
from telegram_membership_saas.modules.projects import (
    Permission,
    Project,
    ProjectMember,
    ProjectMemberRole,
    has_permission,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Payment request timestamps must be timezone-aware")


async def _require_project(session: AsyncSession, project_id: UUID) -> None:
    visible_project = await session.scalar(select(Project.id).where(Project.id == project_id))
    if visible_project is None:
        raise ValueError("Project is not accessible in the current tenant scope")


async def get_or_create_project_customer(
    session: AsyncSession,
    *,
    project_id: UUID,
    user_id: UUID,
) -> ProjectCustomer:
    await _require_project(session, project_id)

    statement = (
        insert(ProjectCustomer)
        .values(project_id=project_id, user_id=user_id)
        .on_conflict_do_nothing(index_elements=["project_id", "user_id"])
        .returning(ProjectCustomer.id)
    )
    customer_id = await session.scalar(statement)

    if customer_id is None:
        customer_id = await session.scalar(
            select(ProjectCustomer.id).where(
                ProjectCustomer.project_id == project_id,
                ProjectCustomer.user_id == user_id,
            )
        )
    if customer_id is None:
        raise RuntimeError("Project customer could not be resolved")

    customer = await session.scalar(
        select(ProjectCustomer).where(ProjectCustomer.id == customer_id)
    )
    if customer is None:
        raise RuntimeError("Project customer disappeared after creation")
    return customer


async def create_payment_request(
    session: AsyncSession,
    *,
    project_id: UUID,
    customer_id: UUID,
    plan_id: UUID,
    plan_price_id: UUID,
    payment_method_id: UUID,
) -> PaymentRequest:
    customer_exists = await session.scalar(
        select(ProjectCustomer.id).where(
            ProjectCustomer.id == customer_id,
            ProjectCustomer.project_id == project_id,
        )
    )
    if customer_exists is None:
        raise ValueError("Customer is not accessible in the current project scope")

    price = await session.scalar(
        select(PlanPrice)
        .join(
            Plan,
            and_(
                Plan.id == PlanPrice.plan_id,
                Plan.project_id == PlanPrice.project_id,
            ),
        )
        .join(CurrencyAsset, CurrencyAsset.code == PlanPrice.currency_code)
        .where(
            PlanPrice.id == plan_price_id,
            PlanPrice.project_id == project_id,
            PlanPrice.plan_id == plan_id,
            PlanPrice.status == PlanPriceStatus.ACTIVE.value,
            Plan.status == PlanStatus.ACTIVE.value,
            CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
        )
    )
    if price is None:
        raise ValueError("Selected plan price is not currently purchasable")

    method_exists = await session.scalar(
        select(PaymentMethod.id)
        .join(
            PaymentMethodCurrency,
            and_(
                PaymentMethodCurrency.payment_method_id == PaymentMethod.id,
                PaymentMethodCurrency.project_id == PaymentMethod.project_id,
            ),
        )
        .where(
            PaymentMethod.id == payment_method_id,
            PaymentMethod.project_id == project_id,
            PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
            PaymentMethodCurrency.currency_code == price.currency_code,
        )
    )
    if method_exists is None:
        raise ValueError("Payment method does not support the selected currency")

    request = PaymentRequest(
        project_id=project_id,
        customer_id=customer_id,
        plan_id=plan_id,
        plan_price_id=plan_price_id,
        payment_method_id=payment_method_id,
        currency_code=price.currency_code,
        regular_amount=price.amount,
        discount_amount=0,
        final_amount=price.amount,
        promotion_id=None,
        status=PaymentRequestStatus.WAITING_CONFIRMATION.value,
    )
    session.add(request)
    await session.flush()
    return request


async def _load_request_for_update(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
) -> PaymentRequest:
    request = await session.scalar(
        select(PaymentRequest)
        .where(
            PaymentRequest.id == request_id,
            PaymentRequest.project_id == project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if request is None:
        raise ValueError("Payment request is not accessible in the current project scope")
    return request


async def mark_payment_request_submitted(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
    submitted_at: datetime | None = None,
) -> PaymentRequest:
    request = await _load_request_for_update(
        session,
        project_id=project_id,
        request_id=request_id,
    )
    if request.status == PaymentRequestStatus.WAITING_REVIEW.value:
        return request
    if request.status != PaymentRequestStatus.WAITING_CONFIRMATION.value:
        raise ValueError("Payment request cannot be submitted from its current state")

    moment = submitted_at or _utcnow()
    _require_aware(moment)
    if moment < request.created_at:
        raise ValueError("submitted_at cannot precede request creation")

    request.status = PaymentRequestStatus.WAITING_REVIEW.value
    request.submitted_at = moment
    await session.flush()
    return request


async def _require_payment_permission(
    session: AsyncSession,
    *,
    project_id: UUID,
    actor_user_id: UUID,
    permission: Permission,
) -> None:
    project = await session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise ValueError("Project is not accessible in the current tenant scope")

    is_owner = project.owner_user_id == actor_user_id
    role_value = None
    if not is_owner:
        role_value = await session.scalar(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == actor_user_id,
            )
        )
    role = ProjectMemberRole(role_value) if role_value is not None else None
    if not has_permission(role, permission, is_owner=is_owner):
        raise PermissionError(f"Missing required permission: {permission.value}")


async def reject_payment_request(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
    reviewer_user_id: UUID,
    reviewed_at: datetime | None = None,
) -> PaymentRequest:
    await _require_payment_permission(
        session,
        project_id=project_id,
        actor_user_id=reviewer_user_id,
        permission=Permission.PAYMENT_REJECT,
    )
    request = await _load_request_for_update(
        session,
        project_id=project_id,
        request_id=request_id,
    )
    if request.status == PaymentRequestStatus.REJECTED.value:
        return request
    if request.status != PaymentRequestStatus.WAITING_REVIEW.value:
        raise ValueError("Only a request waiting for review can be rejected")

    moment = reviewed_at or _utcnow()
    _require_aware(moment)
    if request.submitted_at is None or moment < request.submitted_at:
        raise ValueError("reviewed_at cannot precede submission")

    request.status = PaymentRequestStatus.REJECTED.value
    request.reviewed_at = moment
    request.reviewed_by = reviewer_user_id
    await session.flush()
    return request


async def cancel_payment_request(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
    customer_id: UUID,
) -> PaymentRequest:
    request = await _load_request_for_update(
        session,
        project_id=project_id,
        request_id=request_id,
    )
    if request.customer_id != customer_id:
        raise PermissionError("Payment request does not belong to this customer")
    if request.status == PaymentRequestStatus.CANCELLED.value:
        return request
    if request.status != PaymentRequestStatus.WAITING_CONFIRMATION.value:
        raise ValueError("Only an unsubmitted payment request can be cancelled")

    request.status = PaymentRequestStatus.CANCELLED.value
    await session.flush()
    return request


async def expire_payment_request(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
) -> PaymentRequest:
    request = await _load_request_for_update(
        session,
        project_id=project_id,
        request_id=request_id,
    )
    if request.status == PaymentRequestStatus.EXPIRED.value:
        return request
    if request.status != PaymentRequestStatus.WAITING_CONFIRMATION.value:
        raise ValueError("Only an unsubmitted payment request can expire")

    request.status = PaymentRequestStatus.EXPIRED.value
    await session.flush()
    return request
