from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.pricing.models import PlanPrice, PlanPriceStatus


def _normalize_amount(amount: Decimal | int | str) -> Decimal:
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if value <= 0:
        raise ValueError("Price amount must be positive")
    return value


async def _lock_plan(session: AsyncSession, *, project_id: UUID, plan_id: UUID) -> None:
    locked_plan = await session.scalar(
        select(Plan.id)
        .where(Plan.id == plan_id, Plan.project_id == project_id)
        .with_for_update()
    )
    if locked_plan is None:
        raise ValueError("Plan is not accessible in the current project scope")


async def replace_active_plan_price(
    session: AsyncSession,
    *,
    project_id: UUID,
    plan_id: UUID,
    currency_code: str,
    amount: Decimal | int | str,
    changed_at: datetime | None = None,
) -> PlanPrice:
    """Create the next immutable price version and retire the previous active version.

    The caller owns the surrounding transaction. Locking the Plan serializes all price
    changes for that Plan, so version allocation and active-pair replacement are safe.
    """

    normalized_amount = _normalize_amount(amount)
    timestamp = changed_at or datetime.now(timezone.utc)
    code = currency_code.upper()

    await _lock_plan(session, project_id=project_id, plan_id=plan_id)

    current = await session.scalar(
        select(PlanPrice)
        .where(
            PlanPrice.plan_id == plan_id,
            PlanPrice.project_id == project_id,
            PlanPrice.currency_code == code,
            PlanPrice.status == PlanPriceStatus.ACTIVE.value,
        )
        .with_for_update()
    )

    max_version = await session.scalar(
        select(func.max(PlanPrice.version)).where(
            PlanPrice.plan_id == plan_id,
            PlanPrice.project_id == project_id,
            PlanPrice.currency_code == code,
        )
    )

    if current is not None:
        current.status = PlanPriceStatus.RETIRED.value
        current.active_until = timestamp
        await session.flush()

    new_price = PlanPrice(
        project_id=project_id,
        plan_id=plan_id,
        currency_code=code,
        amount=normalized_amount,
        version=(max_version or 0) + 1,
        status=PlanPriceStatus.ACTIVE.value,
        active_from=timestamp,
    )
    session.add(new_price)
    await session.flush()
    return new_price


async def retire_active_plan_price(
    session: AsyncSession,
    *,
    project_id: UUID,
    plan_id: UUID,
    currency_code: str,
    retired_at: datetime | None = None,
) -> PlanPrice | None:
    """Remove a currency from the current Plan offer without deleting its history."""

    timestamp = retired_at or datetime.now(timezone.utc)
    code = currency_code.upper()

    await _lock_plan(session, project_id=project_id, plan_id=plan_id)
    current = await session.scalar(
        select(PlanPrice)
        .where(
            PlanPrice.plan_id == plan_id,
            PlanPrice.project_id == project_id,
            PlanPrice.currency_code == code,
            PlanPrice.status == PlanPriceStatus.ACTIVE.value,
        )
        .with_for_update()
    )
    if current is None:
        return None

    current.status = PlanPriceStatus.RETIRED.value
    current.active_until = timestamp
    await session.flush()
    return current
