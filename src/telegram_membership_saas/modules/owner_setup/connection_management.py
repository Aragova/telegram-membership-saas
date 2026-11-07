"""Owner-only central connection lifecycle: rename, safe removal, archive.

An unused project may contain configuration (plans, methods, channels) but
must not contain bot credentials, customer data, financial history, audit
records or access work before it can be physically removed. Projects with
history can instead be archived without modifying that history.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.access.models import (
    AccessAction,
    AccessState,
    EffectiveAccess,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.deletion_service import (
    count_retirement_blockers,
)
from telegram_membership_saas.modules.entitlements import EntitlementGrant
from telegram_membership_saas.modules.identity import ProjectCustomer
from telegram_membership_saas.modules.outbox import OutboxEvent
from telegram_membership_saas.modules.owner_setup.obligation_audit import OwnerObligationAudit
from telegram_membership_saas.modules.owner_setup.transfer_models import (
    ProjectOwnershipTransfer,
)
from telegram_membership_saas.modules.payments import Payment, PaymentRequest
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_access import TelegramJoinTicket
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext

# These are *historical* or still-operational rows, not just currently active
# subscriptions. Never cascade-delete any of them to clean a connection.
PROTECTED_HISTORY = (
    PaymentRequest, Payment, Subscription, EntitlementGrant,
    ProjectCustomer, EffectiveAccess, AccessState, AccessAction,
    TelegramJoinTicket, OutboxEvent, OwnerObligationAudit,
    ProjectOwnershipTransfer,
)


@dataclass(frozen=True, slots=True)
class ConnectionDisposition:
    has_bot: bool
    has_history: bool


async def _require_owned_project(
    session: AsyncSession, *, actor_id: UUID, project_id: UUID,
    lock: bool = False,
) -> Project:
    query = select(Project).where(
        Project.id == project_id, Project.owner_user_id == actor_id,
    )
    if lock:
        query = query.with_for_update()
    project = await session.scalar(query)
    if project is None:
        raise PermissionError("Управление подключением доступно только владельцу")
    return project


async def _has_data(session: AsyncSession, model, project_id: UUID) -> bool:
    return (await session.scalar(
        select(func.count()).select_from(model).where(
            model.project_id == project_id,
        )
    )) > 0


async def _disposition(
    session: AsyncSession, project_id: UUID,
) -> ConnectionDisposition:
    has_bot = await _has_data(session, BotInstance, project_id)
    has_history = False
    for model in PROTECTED_HISTORY:
        if await _has_data(session, model, project_id):
            has_history = True
            break
    return ConnectionDisposition(has_bot=has_bot, has_history=has_history)


async def get_connection_disposition(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> ConnectionDisposition:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_owned_project(
            session, actor_id=actor_id, project_id=project_id,
        )
        return await _disposition(session, project_id)


async def rename_owner_connection(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, name: str,
) -> str:
    name = name.strip()
    if not 2 <= len(name) <= 120 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("Название подключения: от 2 до 120 символов, в одну строку")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        project = await _require_owned_project(
            session, actor_id=actor_id, project_id=project_id, lock=True,
        )
        if project.status != "active":
            raise ValueError("Переименовать можно только активное подключение")
        project.name = name
        await session.flush()
    return name


async def remove_unused_connection(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> None:
    """Permanently remove only a disconnected project with ZERO history.

    Locks serialize this decision with concurrent bot registration and
    checkout; the database cascades *configuration only*. Historical and
    operational rows are a hard stop even if already settled or expired.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await _require_owned_project(
            session, actor_id=actor_id, project_id=project_id, lock=True,
        )
        if project.status != "active":
            raise ValueError("Подключение уже удалено или архивировано")
        disposition = await _disposition(session, project_id)
        if disposition.has_bot:
            raise ValueError(
                "Сначала безопасно отключите Telegram-бота, "
                "затем повторите удаление подключения."
            )
        if disposition.has_history:
            raise ValueError(
                "В подключении есть история покупателей, платежей или доступа. "
                "Удалять её нельзя. Можно убрать подключение в архив."
            )
        await session.delete(project)
        await session.flush()


async def archive_owner_connection(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> None:
    """Hide a disconnected project while retaining all immutable records."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await _require_owned_project(
            session, actor_id=actor_id, project_id=project_id, lock=True,
        )
        if project.status != "active":
            raise ValueError("Подключение не активно")
        if (await _disposition(session, project_id)).has_bot:
            raise ValueError("Сначала отключите Telegram-бота")
        blockers = await count_retirement_blockers(session, project_id=project_id)
        if blockers.has_blockers:
            raise ValueError(
                "Архивация пока невозможна: " + blockers.display()
                + ". Сначала завершите обязательства перед покупателями."
            )
        project.status = "archived"
        await session.flush()


async def list_archived_owner_connections(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
) -> tuple[tuple[UUID, str], ...]:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        return tuple((await session.execute(select(Project.id, Project.name).where(
            Project.owner_user_id == actor_id,
            Project.status == "archived",
        ).order_by(Project.created_at, Project.id))).all())


async def restore_owner_connection(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> str:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        project = await _require_owned_project(
            session, actor_id=actor_id, project_id=project_id, lock=True,
        )
        if project.status != "archived":
            raise ValueError("Подключение не находится в архиве")
        project.status = "active"
        await session.flush()
        return project.name
