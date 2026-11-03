from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.pricing.models import (
    CurrencyAsset,
    CurrencyAssetStatus,
    PlanPrice,
    PlanPriceStatus,
)

PRICE_STORAGE_SCALE = 18
PRICE_STORAGE_INTEGER_DIGITS = 20


def _normalize_amount(
    amount: Decimal | int | str,
    *,
    max_fractional_digits: int = PRICE_STORAGE_SCALE,
) -> Decimal:
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Price amount must be a valid decimal number") from exc

    if not value.is_finite() or value <= 0:
        raise ValueError("Price amount must be finite and positive")

    _, digits_tuple, exponent = value.as_tuple()
    digits = list(digits_tuple)

    # Extra fractional zeroes do not change the value and can be stored exactly.
    while exponent < 0 and digits and digits[-1] == 0:
        digits.pop()
        exponent += 1

    fractional_digits = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 0)

    allowed_fractional_digits = min(max_fractional_digits, PRICE_STORAGE_SCALE)
    if fractional_digits > allowed_fractional_digits:
        raise ValueError(
            f"Price amount supports at most {allowed_fractional_digits} fractional digits"
        )
    if integer_digits > PRICE_STORAGE_INTEGER_DIGITS:
        raise ValueError(
            f"Price amount supports at most {PRICE_STORAGE_INTEGER_DIGITS} integer digits"
        )
    return value


def _effective_timestamp(
    explicit: datetime | None,
    *,
    not_before: datetime | None = None,
) -> datetime:
    timestamp = explicit or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Price validity timestamp must be timezone-aware")
    if not_before is not None and timestamp < not_before:
        raise ValueError("Price validity timestamp cannot precede existing price history")
    return timestamp


async def _lock_plan(session: AsyncSession, *, project_id: UUID, plan_id: UUID) -> None:
    locked_plan = await session.scalar(
        select(Plan.id)
        .where(Plan.id == plan_id, Plan.project_id == project_id)
        .with_for_update()
    )
    if locked_plan is None:
        raise ValueError("Plan is not accessible in the current project scope")


async def _currency_precision(session: AsyncSession, currency_code: str) -> int:
    precision = await session.scalar(
        select(CurrencyAsset.precision).where(
            CurrencyAsset.code == currency_code,
            CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
        )
    )
    if precision is None:
        raise ValueError(f"Currency {currency_code} is unknown or disabled")
    return precision


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
    changes for that Plan, so version allocation and validity windows are monotonic.
    """

    code = currency_code.upper()
    precision = await _currency_precision(session, code)
    normalized_amount = _normalize_amount(amount, max_fractional_digits=precision)

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
    latest = await session.scalar(
        select(PlanPrice)
        .where(
            PlanPrice.plan_id == plan_id,
            PlanPrice.project_id == project_id,
            PlanPrice.currency_code == code,
        )
        .order_by(PlanPrice.version.desc())
        .limit(1)
        .with_for_update()
    )

    history_boundary: datetime | None = None
    if current is not None:
        history_boundary = current.active_from
    elif latest is not None:
        history_boundary = latest.active_until or latest.active_from

    # Generate the default time only after the Plan lock is held. This prevents a
    # request that waited longer for the lock from receiving an earlier validity time.
    timestamp = _effective_timestamp(changed_at, not_before=history_boundary)

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

    timestamp = _effective_timestamp(retired_at, not_before=current.active_from)
    current.status = PlanPriceStatus.RETIRED.value
    current.active_until = timestamp
    await session.flush()
    return current
