"""Tenant-safe management of existing plans and their financial history.

Archiving removes a plan from future checkout, not from historical purchases.
Hard deletion is permitted only when *no* request, payment or subscription
references the plan; database FK constraints remain the final safety net.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.payments import Payment, PaymentRequest
from telegram_membership_saas.modules.plans import Plan, PlanResource, PlanStatus
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext


async def plan_has_history(
    session: AsyncSession, *, project_id: UUID, plan_id: UUID,
) -> bool:
    """Any historical sale or access blocks permanent removal, forever."""
    for model in (PaymentRequest, Payment, Subscription):
        ref = await session.scalar(
            select(model.id).where(
                model.project_id == project_id,
                model.plan_id == plan_id,
            ).limit(1)
        )
        if ref is not None:
            return True
    return False


async def _plan_for_owner(
    session: AsyncSession, *, project_id: UUID, actor_id: UUID, plan_id: UUID,
) -> Plan:
    await _require_project_manager(
        session, project_id=project_id, actor_user_id=actor_id,
    )
    plan = await session.scalar(
        select(Plan).where(
            Plan.id == plan_id, Plan.project_id == project_id,
        ).with_for_update()
    )
    if plan is None:
        raise ValueError("Тариф не найден в этом проекте")
    return plan


async def rename_owner_plan(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID, name: str,
) -> str:
    title = name.strip()
    if not 2 <= len(title) <= 120:
        raise ValueError("Название тарифа: от 2 до 120 символов")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        plan = await _plan_for_owner(
            session, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        plan.name = title
        await session.flush()
        return title


async def edit_owner_draft_duration(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID,
    duration_value: int | None, duration_unit: str,
) -> None:
    if duration_unit not in ("day", "week", "month", "year", "lifetime"):
        raise ValueError("Неподдерживаемая длительность")
    if (duration_unit == "lifetime") != (duration_value is None):
        raise ValueError("Укажите корректный срок тарифа")
    if duration_value is not None and not 1 <= duration_value <= 365:
        raise ValueError("Неверный срок тарифа")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        plan = await _plan_for_owner(
            session, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        if plan.status != PlanStatus.DRAFT.value:
            raise ValueError("Срок можно изменить только у неопубликованного тарифа")
        plan.duration_value = duration_value
        plan.duration_unit = duration_unit
        await session.flush()


async def edit_owner_draft_resource(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID, resource_id: UUID,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        plan = await _plan_for_owner(
            session, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        if plan.status != PlanStatus.DRAFT.value:
            raise ValueError("Канал можно изменить только у неопубликованного тарифа")
        resource = await session.scalar(
            select(Resource.id).where(
                Resource.id == resource_id,
                Resource.project_id == project_id,
                Resource.status == ResourceStatus.ACTIVE.value,
            )
        )
        if resource is None:
            raise ValueError("Выберите действующий канал или группу")
        await session.execute(
            delete(PlanResource).where(
                PlanResource.plan_id == plan_id,
                PlanResource.project_id == project_id,
            )
        )
        session.add(PlanResource(
            project_id=project_id, plan_id=plan_id, resource_id=resource_id,
        ))
        await session.flush()


async def archive_owner_plan(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID,
) -> str:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        # Checkout and archive must be ordered so one cannot be created using
        # a stale offer after the archive operation commits.
        await checkout_retirement_lock(session, project_id)
        plan = await _plan_for_owner(
            session, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        if plan.status != PlanStatus.ACTIVE.value:
            raise ValueError("Снять с продажи можно только активный тариф")
        plan.status = PlanStatus.ARCHIVED.value
        await session.flush()
        return plan.name


async def delete_unused_owner_plan(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID,
    expected_status: str,
) -> str:
    """Permanent deletion only after explicit same-status confirmation."""
    if expected_status not in (
        PlanStatus.ACTIVE.value,
        PlanStatus.DRAFT.value,
        PlanStatus.ARCHIVED.value,
    ):
        raise ValueError("Тариф изменился. Откройте карточку заново.")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        plan = await _plan_for_owner(
            session, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        if plan.status != expected_status:
            raise ValueError("Тариф изменился. Откройте карточку заново.")
        if await plan_has_history(
            session, project_id=project_id, plan_id=plan_id,
        ):
            raise ValueError(
                "Тариф имеет историю заявок или подписок. "
                "Удаление запрещено — снимите его с продажи."
            )
        name = plan.name
        try:
            # plan_prices and plan_resources cascade; payments and
            # subscriptions are RESTRICT/NO ACTION in the database.
            await session.delete(plan)
            await session.flush()
        except IntegrityError as exc:
            raise ValueError(
                "Тариф связан с покупками или доступом. Удаление запрещено."
            ) from exc
        return name
