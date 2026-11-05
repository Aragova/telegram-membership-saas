from collections.abc import Iterable
from string import Formatter
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.payments.models import (
    PaymentMethod,
    PaymentMethodCurrency,
    PaymentProvider,
)
from telegram_membership_saas.modules.pricing import CurrencyAsset, CurrencyAssetStatus
from telegram_membership_saas.modules.projects import Project

ALLOWED_INSTRUCTION_FIELDS = frozenset({"amount", "currency", "plan", "period"})
MISSING_NETWORK_PAYMENT_INSTRUCTIONS = (
    "Укажите реквизиты для оплаты перед включением этого способа."
)


def ensure_real_payment_instructions(template: str) -> None:
    """Never expose a network-profile placeholder to a paying buyer."""
    if template.strip() == MISSING_NETWORK_PAYMENT_INSTRUCTIONS:
        raise ValueError("Сначала укажите реальные реквизиты способа оплаты")



def _normalize_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("Payment method name must not be empty")
    if len(normalized) > 120:
        raise ValueError("Payment method name must not exceed 120 characters")
    return normalized


def _normalize_currency_codes(currency_codes: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(
        dict.fromkeys(code.strip().upper() for code in currency_codes if code.strip())
    )
    if not normalized:
        raise ValueError("Payment method must support at least one currency")
    return normalized


def _validate_instruction_template(template: str) -> str:
    normalized = template.strip()
    if not normalized:
        raise ValueError("Payment instruction template must not be empty")

    try:
        parsed = Formatter().parse(normalized)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if field_name not in ALLOWED_INSTRUCTION_FIELDS:
                raise ValueError(f"Unsupported payment instruction field: {field_name}")
            if format_spec:
                raise ValueError("Payment instruction fields do not support format specs")
            if conversion:
                raise ValueError("Payment instruction fields do not support conversions")
    except ValueError as exc:
        if str(exc).startswith(("Unsupported", "Payment instruction")):
            raise
        raise ValueError("Invalid payment instruction template") from exc

    return normalized


async def _require_project(session: AsyncSession, project_id: UUID) -> None:
    visible_project = await session.scalar(select(Project.id).where(Project.id == project_id))
    if visible_project is None:
        raise ValueError("Project is not accessible in the current tenant scope")


async def _require_active_currencies(
    session: AsyncSession,
    currency_codes: tuple[str, ...],
) -> None:
    rows = set(
        (
            await session.scalars(
                select(CurrencyAsset.code).where(
                    CurrencyAsset.code.in_(currency_codes),
                    CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
                )
            )
        ).all()
    )
    missing = sorted(set(currency_codes) - rows)
    if missing:
        raise ValueError(f"Unknown or disabled currencies: {', '.join(missing)}")


def render_manual_instruction(
    template: str,
    *,
    amount: str,
    currency: str,
    plan: str,
    period: str,
) -> str:
    validated = _validate_instruction_template(template)
    return validated.format(amount=amount, currency=currency, plan=plan, period=period)


async def create_manual_payment_method(
    session: AsyncSession,
    *,
    project_id: UUID,
    name: str,
    currency_codes: Iterable[str],
    instruction_template: str,
    sort_order: int = 0,
) -> PaymentMethod:
    if sort_order < 0:
        raise ValueError("Payment method sort order must be non-negative")

    normalized_name = _normalize_name(name)
    codes = _normalize_currency_codes(currency_codes)
    validated_template = _validate_instruction_template(instruction_template)

    await _require_project(session, project_id)
    await _require_active_currencies(session, codes)

    method = PaymentMethod(
        project_id=project_id,
        name=normalized_name,
        provider=PaymentProvider.MANUAL_SETTLEMENT.value,
        instruction_template=validated_template,
        sort_order=sort_order,
    )
    session.add(method)
    await session.flush()

    session.add_all(
        PaymentMethodCurrency(
            payment_method_id=method.id,
            project_id=project_id,
            currency_code=code,
        )
        for code in codes
    )
    await session.flush()
    return method


async def set_payment_method_currencies(
    session: AsyncSession,
    *,
    project_id: UUID,
    payment_method_id: UUID,
    currency_codes: Iterable[str],
) -> PaymentMethod:
    codes = _normalize_currency_codes(currency_codes)
    await _require_active_currencies(session, codes)

    method = await session.scalar(
        select(PaymentMethod)
        .where(PaymentMethod.id == payment_method_id, PaymentMethod.project_id == project_id)
        .with_for_update()
    )
    if method is None:
        raise ValueError("Payment method is not accessible in the current project scope")

    await session.execute(
        delete(PaymentMethodCurrency).where(
            PaymentMethodCurrency.payment_method_id == payment_method_id,
            PaymentMethodCurrency.project_id == project_id,
        )
    )
    session.add_all(
        PaymentMethodCurrency(
            payment_method_id=payment_method_id,
            project_id=project_id,
            currency_code=code,
        )
        for code in codes
    )
    await session.flush()
    return method
