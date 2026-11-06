"""One guarded, project-scoped bulk switch for existing plans/payment methods.

Only future checkout availability changes. Paid customer grants, pending
requests and immutable financial history are never mutated. No schema changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2s
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.payments.models import PaymentMethod, PaymentMethodCurrency
from telegram_membership_saas.modules.payments.service import ensure_real_payment_instructions
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import PlanPrice
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class BulkSalesState:
    kind: str
    active: int
    paused: int
    drafts: int
    revision: str

    @property
    def available(self) -> bool:
        return self.active > 0 or self.paused > 0

    @property
    def enable_action(self) -> bool:
        # One context-sensitive button: when anything is on, pause everything;
        # otherwise restore all eligible archived plans/disabled methods.
        return self.active == 0 and self.paused > 0


async def _rows(session: AsyncSession, project_id: UUID, kind: str, *, lock: bool):
    model = {"plan": Plan, "method": PaymentMethod}.get(kind)
    if model is None:
        raise ValueError("Неизвестный раздел управления продажами")
    query = select(model).where(
        model.project_id == project_id,
    ).order_by(model.id)
    if lock:
        query = query.with_for_update()
    return (await session.scalars(query)).all()


def _state(kind: str, rows) -> BulkSalesState:
    active = sum(item.status == "active" for item in rows)
    paused = sum(item.status == (
        "archived" if kind == "plan" else "disabled"
    ) for item in rows)
    drafts = sum(item.status == "draft" for item in rows) if kind == "plan" else 0
    # Change-bound token to reject stale Telegram confirmation keyboards.
    serialized = "|".join(
        f"{item.id.hex}:{item.status}:{item.updated_at.isoformat()}"
        for item in rows
    )
    revision = blake2s(serialized.encode(), digest_size=6).hexdigest()
    return BulkSalesState(kind, active, paused, drafts, revision)


async def read_bulk_sales_state(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, kind: str,
) -> BulkSalesState:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        return _state(kind, await _rows(session, project_id, kind, lock=False))


async def _require_plan_sale_ready(
    session: AsyncSession, *, project_id: UUID, plan: Plan,
) -> None:
    resource = await session.scalar(
        select(PlanResource.resource_id).join(
            Resource, and_(
                Resource.id == PlanResource.resource_id,
                Resource.project_id == PlanResource.project_id,
            ),
        ).where(
            PlanResource.project_id == project_id,
            PlanResource.plan_id == plan.id,
            Resource.status == "active",
        ).limit(1)
    )
    if resource is None:
        raise ValueError(
            f"Тариф «{plan.name}»: нет действующего канала. "
            "Исправьте тариф и повторите включение."
        )
    available = await session.scalar(
        select(PlanPrice.id).join(
            PaymentMethodCurrency, and_(
                PaymentMethodCurrency.project_id == PlanPrice.project_id,
                PaymentMethodCurrency.currency_code == PlanPrice.currency_code,
            ),
        ).join(
            PaymentMethod, and_(
                PaymentMethod.id == PaymentMethodCurrency.payment_method_id,
                PaymentMethod.project_id == project_id,
                PaymentMethod.status == "active",
            ),
        ).where(
            PlanPrice.project_id == project_id,
            PlanPrice.plan_id == plan.id,
            PlanPrice.status == "active",
        ).limit(1)
    )
    if available is None:
        raise ValueError(
            f"Тариф «{plan.name}»: не настроена цена или действующая оплата. "
            "Исправьте настройки и повторите включение."
        )


async def switch_all_sales(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, kind: str,
    enable: bool, expected_revision: str,
) -> int:
    """Atomic all-or-nothing status change, serialized with buyer checkouts."""
    if len(expected_revision) != 12:
        raise ValueError("Меню устарело. Откройте раздел заново.")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id, lock=True,
        )
        rows = await _rows(session, project_id, kind, lock=True)
        before = _state(kind, rows)
        if (
            before.revision != expected_revision
            or not before.available or before.enable_action != enable
        ):
            raise ValueError(
                "Настройки изменились. Откройте раздел заново и повторите."
            )
        target = "active" if enable else (
            "archived" if kind == "plan" else "disabled"
        )
        source = (
            "archived" if kind == "plan" else "disabled"
        ) if enable else "active"
        affected = [item for item in rows if item.status == source]
        if enable:
            for item in affected:
                if kind == "plan":
                    await _require_plan_sale_ready(
                        session, project_id=project_id, plan=item,
                    )
                else:
                    ensure_real_payment_instructions(item.instruction_template)
        for item in affected:
            item.status = target
            item.updated_at = func.clock_timestamp()
        await session.flush()
        return len(affected)
