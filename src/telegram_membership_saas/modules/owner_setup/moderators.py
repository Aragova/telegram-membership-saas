"""Least-privilege project support staff. Never grants financial/admin privileges."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope, tenant_session_scope
from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.owner_setup.obligation_audit import OwnerObligationAudit
from telegram_membership_saas.modules.payments import PaymentRequest, PaymentRequestStatus
from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.projects import (
    Project,
    ProjectMember,
    ProjectMemberRole,
    ProjectStatus,
)
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext


@dataclass(frozen=True, slots=True)
class ModeratorAccount:
    telegram_id: int
    username: str | None
    first_name: str | None


@dataclass(frozen=True, slots=True)
class ModeratorCustomer:
    id: UUID
    telegram_id: int
    username: str | None
    first_name: str | None
    is_banned: bool
    invoice_count: int


@dataclass(frozen=True, slots=True)
class ModeratorReview:
    id: UUID
    telegram_id: int
    username: str | None
    plan_name: str
    submitted_at: datetime | None


async def _require_owner(
    session: AsyncSession, *, project_id: UUID, actor_id: UUID,
) -> Project:
    project = await session.scalar(
        select(Project).where(Project.id == project_id)
    )
    if (
        project is None or project.status != ProjectStatus.ACTIVE.value
        or project.owner_user_id != actor_id
    ):
        raise PermissionError("Только владелец может управлять модераторами")
    return project


async def _require_support(
    session: AsyncSession, *, project_id: UUID, actor_id: UUID,
) -> Project:
    project = await session.scalar(
        select(Project).where(Project.id == project_id)
    )
    if project is None or project.status != ProjectStatus.ACTIVE.value:
        raise PermissionError("Проект недоступен")
    if project.owner_user_id != actor_id:
        role = await session.scalar(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == actor_id,
            )
        )
        if role != ProjectMemberRole.SUPPORT.value:
            raise PermissionError("Нет прав модератора")
    return project


async def can_use_moderator_panel(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, support_only: bool = False,
) -> bool:
    try:
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id),
        ) as session:
            project = await _require_support(
                session, project_id=project_id, actor_id=actor_id,
            )
            return not support_only or project.owner_user_id != actor_id
    except (PermissionError, TenantAccessDenied):
        return False


async def list_project_moderators(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, page: int = 0,
) -> tuple[int, tuple[ModeratorAccount, ...]]:
    if not 0 <= page <= 10000:
        raise ValueError("Неверная страница")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_owner(session, project_id=project_id, actor_id=actor_id)
        where = (
            ProjectMember.project_id == project_id,
            ProjectMember.role == ProjectMemberRole.SUPPORT.value,
        )
        total = await session.scalar(
            select(func.count(ProjectMember.id)).where(*where)
        ) or 0
        rows = (await session.execute(
            select(TelegramUser.telegram_user_id, TelegramUser.username,
                   TelegramUser.first_name)
            .join(ProjectMember, ProjectMember.user_id == TelegramUser.id)
            .where(*where)
            .order_by(ProjectMember.created_at, ProjectMember.id)
            .offset(page * 8).limit(8)
        )).all()
        return total, tuple(
            ModeratorAccount(telegram_id=tid, username=name, first_name=first)
            for tid, name, first in rows
        )


async def set_project_moderator(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, telegram_id: int, enabled: bool,
) -> bool:
    """Owner-only scoped role assignment; never overwrite stronger roles."""
    if not 0 < telegram_id <= 2**63 - 1:
        raise ValueError("Некорректный Telegram ID")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        project = await _require_owner(
            session, project_id=project_id, actor_id=actor_id,
        )
        account = await session.scalar(
            select(TelegramUser).where(
                TelegramUser.telegram_user_id == telegram_id,
            )
        )
        if account is None:
            raise ValueError("Сначала пользователь должен открыть этого бота через /start")
        if account.id == project.owner_user_id:
            raise ValueError("Владелец уже имеет полные права")
        member = await session.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == account.id,
            ).with_for_update()
        )
        if member is not None and member.role != ProjectMemberRole.SUPPORT.value:
            raise PermissionError("У пользователя уже есть другая роль. Она не изменена")
        if enabled:
            if member is not None:
                return False
            session.add(ProjectMember(
                project_id=project_id, user_id=account.id,
                role=ProjectMemberRole.SUPPORT.value,
            ))
        else:
            if member is None:
                return False
            await session.delete(member)
        session.add(OwnerObligationAudit(
            project_id=project_id, actor_id=actor_id,
            action="moderator_add" if enabled else "moderator_remove",
            target_id=account.id,
            reason="Назначение или отзыв роли support; без финансовых прав",
        ))
        return True


async def find_moderator_customer(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID | None = None,
    telegram_id: int | None = None,
) -> ModeratorCustomer:
    """Read only a buyer's identity, ban status and invoice count."""
    if (customer_id is None) == (telegram_id is None):
        raise ValueError("Укажите одного покупателя")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_support(session, project_id=project_id, actor_id=actor_id)
        stmt = (
            select(ProjectCustomer, TelegramUser)
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .where(ProjectCustomer.project_id == project_id)
        )
        if customer_id is not None:
            stmt = stmt.where(ProjectCustomer.id == customer_id)
        else:
            stmt = stmt.where(TelegramUser.telegram_user_id == telegram_id)
        row = (await session.execute(stmt)).one_or_none()
        if row is None:
            raise ValueError("Покупатель не найден в этом боте")
        customer, user = row
        count = await session.scalar(
            select(func.count(PaymentRequest.id)).where(
                PaymentRequest.project_id == project_id,
                PaymentRequest.customer_id == customer.id,
            )
        ) or 0
        return ModeratorCustomer(
            id=customer.id, telegram_id=user.telegram_user_id,
            username=user.username, first_name=user.first_name,
            is_banned=customer.is_banned, invoice_count=count,
        )


async def list_moderator_customers(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, page: int = 0,
) -> tuple[int, tuple[ModeratorCustomer, ...]]:
    if not 0 <= page <= 10000:
        raise ValueError("Неверная страница")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_support(session, project_id=project_id, actor_id=actor_id)
        where = ProjectCustomer.project_id == project_id
        total = await session.scalar(
            select(func.count(ProjectCustomer.id)).where(where)
        ) or 0
        rows = (await session.execute(
            select(ProjectCustomer.id, TelegramUser.telegram_user_id,
                   TelegramUser.username, TelegramUser.first_name,
                   ProjectCustomer.is_banned)
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .where(where)
            .order_by(ProjectCustomer.created_at.desc(), ProjectCustomer.id.desc())
            .offset(page * 8).limit(8)
        )).all()
        return total, tuple(ModeratorCustomer(
            id=cid, telegram_id=tid, username=name, first_name=first,
            is_banned=banned, invoice_count=0,
        ) for cid, tid, name, first, banned in rows)


async def list_moderator_reviews(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> tuple[ModeratorReview, ...]:
    """Read-only pending-review metadata, no amount, bank details or receipt."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _require_support(session, project_id=project_id, actor_id=actor_id)
        rows = (await session.execute(
            select(PaymentRequest.id, TelegramUser.telegram_user_id,
                   TelegramUser.username, Plan.name, PaymentRequest.submitted_at)
            .join(ProjectCustomer, and_(
                ProjectCustomer.id == PaymentRequest.customer_id,
                ProjectCustomer.project_id == PaymentRequest.project_id,
            ))
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .join(Plan, and_(
                Plan.id == PaymentRequest.plan_id,
                Plan.project_id == PaymentRequest.project_id,
            ))
            .where(
                PaymentRequest.project_id == project_id,
                PaymentRequest.status == PaymentRequestStatus.WAITING_REVIEW.value,
            )
            .order_by(PaymentRequest.submitted_at.desc(), PaymentRequest.id.desc())
            .limit(20)
        )).all()
        return tuple(ModeratorReview(
            id=req, telegram_id=tid, username=username,
            plan_name=plan, submitted_at=submitted,
        ) for req, tid, username, plan, submitted in rows)


async def set_moderated_customer_ban(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, customer_id: UUID, banned: bool,
) -> bool:
    """A support worker may stop spam, but cannot revoke paid entitlements."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        project = await _require_support(
            session, project_id=project_id, actor_id=actor_id,
        )
        if project.owner_user_id == actor_id:
            raise PermissionError("Используйте обычную карточку владельца")
        customer = await session.scalar(
            select(ProjectCustomer).where(
                ProjectCustomer.project_id == project_id,
                ProjectCustomer.id == customer_id,
            ).with_for_update()
        )
        if customer is None:
            raise ValueError("Покупатель не найден")
        if customer.user_id == project.owner_user_id:
            raise PermissionError("Нельзя заблокировать владельца")
        if customer.is_banned == banned:
            return False
        customer.is_banned = banned
        customer.banned_at = datetime.now(UTC) if banned else None
        customer.banned_by_user_id = actor_id if banned else None
        session.add(OwnerObligationAudit(
            project_id=project_id, actor_id=actor_id,
            action="customer_ban" if banned else "customer_unban",
            target_id=customer_id,
            reason="Решение модератора: новые счета/чеки запрещены; оплаченный доступ сохранён",
        ))
        return True


async def project_support_recipients(
    factory: async_sessionmaker[AsyncSession], *, project_id: UUID,
) -> tuple[int, ...]:
    """Trusted project worker notification; no access token is embedded."""
    async with system_project_session_scope(factory, project_id) as session:
        rows = (await session.scalars(
            select(TelegramUser.telegram_user_id)
            .join(ProjectMember, ProjectMember.user_id == TelegramUser.id)
            .join(Project, Project.id == ProjectMember.project_id)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.role == ProjectMemberRole.SUPPORT.value,
                Project.status == ProjectStatus.ACTIVE.value,
            )
        )).all()
        return tuple(rows)
