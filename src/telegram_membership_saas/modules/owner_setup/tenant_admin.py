"""Tenant-scoped owner administration of payment options and tariff readiness.

Payment method links are adjusted without deleting existing methods, prices or
financial transactions. The buyer storefront uses the same compatibility rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.owner_setup.currencies import (
    project_currency_is_selected,
)
from telegram_membership_saas.modules.owner_setup.method_management import (
    method_has_history,
    method_revision,
)
from telegram_membership_saas.modules.owner_setup.plan_management import (
    plan_has_confirmed_purchases,
    plan_has_history,
    plan_revision,
)
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
)
from telegram_membership_saas.modules.payments.service import ensure_real_payment_instructions
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import (
    CurrencyAsset,
    PlanPrice,
    PlanPriceStatus,
)
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.telegram_admin.service import (
    _require_project_manager,
)
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class MethodInfo:
    id: UUID
    name: str
    status: str
    currencies: tuple[str, ...]
    revision: str = ""
    has_history: bool = False
    instruction: str = ""


@dataclass(frozen=True, slots=True)
class PriceInfo:
    currency: str
    amount: Decimal
    precision: int
    method_count: int


@dataclass(frozen=True, slots=True)
class PlanInfo:
    id: UUID
    name: str
    status: str
    duration_value: int | None
    duration_unit: str
    resource_name: str | None
    prices: tuple[PriceInfo, ...]
    has_history: bool = False
    has_purchase: bool = False
    revision: str = ""


METHOD_PAGE_SIZE = 8


async def list_owner_methods(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, page: int = 0,
) -> tuple[MethodInfo, ...]:
    if not 0 <= page <= 10000:
        raise ValueError("Недопустимая страница")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        methods = (await session.scalars(
            select(PaymentMethod)
            .where(PaymentMethod.project_id == project_id)
            .order_by(PaymentMethod.sort_order, PaymentMethod.created_at)
            .offset(page * METHOD_PAGE_SIZE).limit(METHOD_PAGE_SIZE)
        )).all()
        currencies = (await session.execute(
            select(
                PaymentMethodCurrency.payment_method_id,
                PaymentMethodCurrency.currency_code,
            ).where(
                PaymentMethodCurrency.project_id == project_id,
                PaymentMethodCurrency.payment_method_id.in_(
                    [method.id for method in methods]
                ),
            )
        )).all()
        by_method: dict[UUID, list[str]] = {}
        for method_id, code in currencies:
            by_method.setdefault(method_id, []).append(code)
        return tuple(
            MethodInfo(
                id=method.id, name=method.name, status=method.status,
                currencies=tuple(sorted(by_method.get(method.id, ()))),
            )
            for method in methods
        )


async def get_owner_method_info(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, method_id: UUID,
) -> MethodInfo:
    """Retrieve one method independently of the owner's list pagination."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        method = await session.scalar(
            select(PaymentMethod).where(
                PaymentMethod.id == method_id,
                PaymentMethod.project_id == project_id,
            )
        )
        if method is None:
            raise ValueError("Способ оплаты не найден в этом проекте")
        currencies = (await session.scalars(
            select(PaymentMethodCurrency.currency_code)
            .where(
                PaymentMethodCurrency.project_id == project_id,
                PaymentMethodCurrency.payment_method_id == method_id,
            )
            .order_by(PaymentMethodCurrency.currency_code)
        )).all()
        return MethodInfo(
            id=method.id, name=method.name, status=method.status,
            currencies=tuple(currencies),
            revision=method_revision(method),
            has_history=await method_has_history(
                session, project_id=project_id, method_id=method_id,
            ),
            instruction=method.instruction_template,
        )


async def attach_owner_method_currency(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
    method_id: UUID, currency_code: str,
) -> bool:
    currency_code = currency_code.strip().upper()
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        method = await session.scalar(
            select(PaymentMethod)
            .where(
                PaymentMethod.id == method_id,
                PaymentMethod.project_id == project_id,
            ).with_for_update()
        )
        if method is None:
            raise ValueError("Способ оплаты недоступен в этом проекте")
        if not await project_currency_is_selected(
            session, project_id=project_id, code=currency_code,
        ):
            raise ValueError("Сначала добавьте валюту в этом проекте")
        outcome = await session.execute(
            insert(PaymentMethodCurrency)
            .values(
                payment_method_id=method_id,
                project_id=project_id,
                currency_code=currency_code,
            )
            .on_conflict_do_nothing(
                index_elements=["payment_method_id", "currency_code"],
            )
            .returning(PaymentMethodCurrency.currency_code)
        )
        added = outcome.scalar_one_or_none() is not None
        if added:
            method.updated_at = func.clock_timestamp()
            await session.flush()
        return added


async def change_owner_method_status(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
    method_id: UUID, enabled: bool,
) -> bool:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        method = await session.scalar(
            select(PaymentMethod).where(
                PaymentMethod.id == method_id,
                PaymentMethod.project_id == project_id,
            ).with_for_update()
        )
        if method is None:
            raise ValueError("Способ оплаты не найден")
        next_status = "active" if enabled else "disabled"
        if method.status == next_status:
            return False
        if enabled:
            ensure_real_payment_instructions(method.instruction_template)
        method.status = next_status
        await session.flush()
        return True


async def get_owner_plan_info(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID,
) -> PlanInfo:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        plan = await session.scalar(
            select(Plan).where(
                Plan.id == plan_id, Plan.project_id == project_id,
                Plan.removed_at.is_(None),
            )
        )
        if plan is None:
            raise ValueError("Тариф не найден в этом проекте")
        resource_name = await session.scalar(
            select(Resource.name)
            .join(
                PlanResource,
                and_(
                    PlanResource.resource_id == Resource.id,
                    PlanResource.project_id == Resource.project_id,
                ),
            )
            .where(
                PlanResource.project_id == project_id,
                PlanResource.plan_id == plan_id,
            ).limit(1)
        )
        prices = (await session.execute(
            select(
                PlanPrice.currency_code, PlanPrice.amount,
                CurrencyAsset.precision,
            )
            .join(CurrencyAsset, CurrencyAsset.code == PlanPrice.currency_code)
            .where(
                PlanPrice.project_id == project_id,
                PlanPrice.plan_id == plan_id,
                PlanPrice.status == PlanPriceStatus.ACTIVE.value,
            )
            .order_by(PlanPrice.currency_code)
        )).all()
        active_methods = (await session.execute(
            select(
                PaymentMethodCurrency.currency_code,
                func.count(PaymentMethod.id),
            )
            .join(
                PaymentMethod,
                and_(
                    PaymentMethod.id == PaymentMethodCurrency.payment_method_id,
                    PaymentMethod.project_id == PaymentMethodCurrency.project_id,
                ),
            )
            .where(
                PaymentMethodCurrency.project_id == project_id,
                PaymentMethod.status == "active",
            )
            .group_by(PaymentMethodCurrency.currency_code)
        )).all()
        method_counts = dict(active_methods)
        has_history = await plan_has_history(
            session, project_id=project_id, plan_id=plan_id,
        )
        return PlanInfo(
            id=plan.id, name=plan.name, status=plan.status,
            duration_value=plan.duration_value, duration_unit=plan.duration_unit,
            resource_name=resource_name,
            has_history=has_history,
            has_purchase=await plan_has_confirmed_purchases(
                session, project_id=project_id, plan_id=plan_id,
            ),
            revision=plan_revision(plan),
            prices=tuple(
                PriceInfo(
                    currency=code, amount=amount, precision=precision,
                    method_count=int(method_counts.get(code, 0)),
                )
                for code, amount, precision in prices
            ),
        )
