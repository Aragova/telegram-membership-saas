"""Tenant-safe owner management of existing manual payment methods.

Current buyer checkout and method retirement share a project advisory lock.
Historical requests hold a database FK; deleting a used method is forbidden.
"""
from __future__ import annotations

from hashlib import blake2s
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.payments.models import (
    Payment,
    PaymentMethod,
    PaymentMethodCurrency,
    PaymentRequest,
)
from telegram_membership_saas.modules.payments.service import (
    _validate_instruction_template,
    ensure_real_payment_instructions,
)
from telegram_membership_saas.modules.telegram_admin.service import (
    _require_project_manager,
)
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext


def method_revision(method: PaymentMethod) -> str:
    if method.updated_at is None:
        raise ValueError("Способ оплаты изменился. Откройте карточку заново.")
    return blake2s(
        method.updated_at.isoformat().encode("utf-8"), digest_size=6,
    ).hexdigest()


async def method_has_history(
    session: AsyncSession, *, project_id: UUID, method_id: UUID,
) -> bool:
    for model in (PaymentRequest, Payment):
        if await session.scalar(select(model.id).where(
            model.project_id == project_id,
            model.payment_method_id == method_id,
        ).limit(1)) is not None:
            return True
    return False


async def _owner_method(
    session: AsyncSession, *, actor_id: UUID,
    project_id: UUID, method_id: UUID,
) -> PaymentMethod:
    await _require_project_manager(
        session, project_id=project_id, actor_user_id=actor_id,
    )
    method = await session.scalar(
        select(PaymentMethod).where(
            PaymentMethod.project_id == project_id, PaymentMethod.id == method_id,
        ).with_for_update()
    )
    if method is None:
        raise ValueError("Способ оплаты не найден в этом проекте")
    return method


async def rename_owner_method(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, method_id: UUID, name: str,
) -> str:
    title = name.strip()
    if not 2 <= len(title) <= 120:
        raise ValueError("Название способа оплаты: от 2 до 120 символов")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        method = await _owner_method(
            session, actor_id=actor_id, project_id=project_id, method_id=method_id,
        )
        method.name = title
        method.updated_at = func.clock_timestamp()
        await session.flush()
        return title


async def edit_owner_method_instructions(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, method_id: UUID, instruction: str,
) -> None:
    if len(instruction) > 4000:
        raise ValueError("Инструкция слишком длинная (максимум 4000 символов)")
    validated = _validate_instruction_template(instruction)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        method = await _owner_method(
            session, actor_id=actor_id, project_id=project_id, method_id=method_id,
        )
        method.instruction_template = validated
        method.updated_at = func.clock_timestamp()
        await session.flush()


async def change_owner_method_status_guarded(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, method_id: UUID,
    enabled: bool, expected_revision: str,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        method = await _owner_method(
            session, actor_id=actor_id, project_id=project_id, method_id=method_id,
        )
        status = "disabled" if enabled else "active"
        if method.status != status or method_revision(method) != expected_revision:
            raise ValueError("Способ оплаты изменился. Откройте карточку заново.")
        if enabled:
            ensure_real_payment_instructions(method.instruction_template)
        method.status = "active" if enabled else "disabled"
        method.updated_at = func.clock_timestamp()
        await session.flush()


async def detach_owner_method_currency(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, method_id: UUID,
    currency_code: str, expected_revision: str,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        method = await _owner_method(
            session, actor_id=actor_id, project_id=project_id, method_id=method_id,
        )
        if method_revision(method) != expected_revision:
            raise ValueError("Способ оплаты изменился. Откройте карточку заново.")
        codes = (await session.scalars(
            select(PaymentMethodCurrency.currency_code).where(
                PaymentMethodCurrency.project_id == project_id,
                PaymentMethodCurrency.payment_method_id == method_id,
            )
        )).all()
        if currency_code not in codes:
            raise ValueError("Валюта уже отключена")
        if len(codes) <= 1:
            raise ValueError("Нельзя удалить последнюю валюту. Отключите способ оплаты.")
        await session.execute(delete(PaymentMethodCurrency).where(
            PaymentMethodCurrency.project_id == project_id,
            PaymentMethodCurrency.payment_method_id == method_id,
            PaymentMethodCurrency.currency_code == currency_code,
        ))
        method.updated_at = func.clock_timestamp()
        await session.flush()


async def delete_unused_owner_method(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, method_id: UUID,
    expected_revision: str,
) -> str:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        method = await _owner_method(
            session, actor_id=actor_id, project_id=project_id, method_id=method_id,
        )
        if method_revision(method) != expected_revision:
            raise ValueError("Способ оплаты изменился. Откройте карточку заново.")
        if await method_has_history(
            session, project_id=project_id, method_id=method_id,
        ):
            raise ValueError(
                "По этому способу есть история покупок. "
                "Удаление запрещено — отключите его для новых покупок."
            )
        name = method.name
        try:
            await session.delete(method)
            await session.flush()
        except IntegrityError as exc:
            raise ValueError("Способ оплаты связан с покупками. Удаление запрещено.") from exc
        return name
