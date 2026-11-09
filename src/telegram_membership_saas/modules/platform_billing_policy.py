"""Pure, side-effect-free ViMi Pay commission calculation.

This module does NOT create an invoice, collect a fee, enable SaaS billing,
enforce a quota, or modify any customer payment. Callers must provide frozen,
approved subscription/file purchase snapshots and an authorized billing policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

RUB = "RUB"
PLATFORM_SERVICE_FEE_RUB = Decimal("150.00")
PLATFORM_COMMISSION_RATE = Decimal("0.05")
PLATFORM_TRIAL_HOURS = 72
PLATFORM_FREE_GRANT_MONTHLY_QUOTA = 3
MONEY_QUANTUM = Decimal("0.01")
MOSCOW = ZoneInfo("Europe/Moscow")

SaleKind = Literal["subscription", "file"]


@dataclass(frozen=True, slots=True)
class ApprovedSaleSnapshot:
    """Immutable billing inputs from approved Payment or FileManualOrder."""

    source_kind: SaleKind
    source_id: UUID
    merchant_id: UUID
    approved_at: datetime
    amount: Decimal
    currency_code: str


@dataclass(frozen=True, slots=True)
class CommissionLine:
    source_kind: SaleKind
    source_id: UUID
    merchant_id: UUID
    approved_at: datetime
    original_amount: Decimal
    currency_code: str
    rub_per_unit: Decimal
    gross_rub: Decimal
    commission_rub: Decimal


def _positive_decimal(value: Decimal, *, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{label} must be a finite, positive Decimal")
    return value


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must have a timezone")
    return value


def calculate_commission_lines(
    approved_sales: tuple[ApprovedSaleSnapshot, ...],
    *,
    billing_month: date,
    rub_per_unit: dict[str, Decimal],
    billable_from: datetime | None = None,
    commission_rate: Decimal = PLATFORM_COMMISSION_RATE,
) -> tuple[CommissionLine, ...]:
    """Calculate five-percent lines without changing financial source records.

    billing_month must be the FIRST calendar day of a Moscow billing month.
    Rates are explicitly supplied by the billing operator, per currency and
    month, and must later be snapshotted into immutable invoice-line records.
    RUB uses rate 1. Missing foreign FX fails, never silently charges zero.
    Duplicate source IDs within a kind are a hard error for the caller.
    """
    if billing_month.day != 1:
        raise ValueError("billing_month must be the first of the month")
    if not isinstance(commission_rate, Decimal) or not (
        commission_rate.is_finite() and Decimal("0") <= commission_rate <= Decimal("1")
    ):
        raise ValueError("commission_rate must be a Decimal in [0,1]")
    if billable_from is not None:
        _aware(billable_from, label="billable_from")

    seen: set[tuple[str, UUID]] = set()
    lines: list[CommissionLine] = []
    for sale in approved_sales:
        if sale.source_kind not in ("subscription", "file"):
            raise ValueError("Unknown approved purchase source")
        _aware(sale.approved_at, label="approved_at")
        _positive_decimal(sale.amount, label="sale amount")
        currency = sale.currency_code.strip().upper()
        if not currency or len(currency) > 16:
            raise ValueError("Invalid sale currency")
        # Invoice periods are Moscow calendar months; use the source's
        # original approval timestamp, not the time we generate a statement.
        local = sale.approved_at.astimezone(MOSCOW)
        if (local.year, local.month) != (billing_month.year, billing_month.month):
            continue
        if billable_from is not None and sale.approved_at < billable_from:
            continue
        key = (sale.source_kind, sale.source_id)
        if key in seen:
            raise ValueError(f"Duplicate approved purchase source: {key}")
        seen.add(key)
        rate = (
            Decimal("1")
            if currency == RUB
            else rub_per_unit.get(currency)
        )
        if rate is None:
            raise ValueError(f"Missing configured RUB conversion rate for {currency}")
        rate = _positive_decimal(rate, label=f"RUB conversion rate for {currency}")
        # Numeric(38,18) snapshots and operator-supplied FX rates can exceed
        # the default Decimal precision of 28. Avoid intermediate rounding
        # before the explicit final kopeck rounding, regardless of the
        # caller's ambient decimal context.
        operands = (sale.amount, rate, commission_rate)
        needed = sum(
            max(len(value.as_tuple().digits), value.adjusted() + 2)
            for value in operands
        ) + 12
        with localcontext() as context:
            context.prec = max(80, needed)
            gross_rub = (sale.amount * rate).quantize(
                MONEY_QUANTUM, rounding=ROUND_HALF_UP,
            )
            commission_rub = (sale.amount * rate * commission_rate).quantize(
                MONEY_QUANTUM, rounding=ROUND_HALF_UP,
            )
        lines.append(
            CommissionLine(
                source_kind=sale.source_kind,
                source_id=sale.source_id,
                merchant_id=sale.merchant_id,
                approved_at=sale.approved_at,
                original_amount=sale.amount,
                currency_code=currency,
                rub_per_unit=rate,
                gross_rub=gross_rub,
                commission_rub=commission_rub,
            )
        )
    return tuple(lines)


def total_commission_rub(lines: tuple[CommissionLine, ...]) -> Decimal:
    """Sum already rounded line fees; invoice generation stays separate."""
    amounts = tuple(line.commission_rub for line in lines)
    # Addition itself can round under a low-precision ambient context.
    largest = max(
        (max(len(value.as_tuple().digits), value.adjusted() + 2) for value in amounts),
        default=2,
    )
    with localcontext() as context:
        context.prec = max(80, largest + len(str(len(amounts) or 1)) + 12)
        return sum(amounts, Decimal("0.00"))
