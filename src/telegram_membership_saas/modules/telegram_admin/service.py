"""Project owner/admin Telegram application services (project-scoped and authorized)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID
from zoneinfo import available_timezones

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.payments import PaymentMethod
from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.projects import (
    Permission,
    Project,
    ProjectMember,
    ProjectMemberRole,
    ProjectStatus,
    has_permission,
)
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext


@dataclass(frozen=True, slots=True)
class AdminEntry:
    name: str
    status: str
    details: str = ""


@dataclass(frozen=True, slots=True)
class AdminProjectSnapshot:
    project_name: str
    timezone: str
    resources: tuple[AdminEntry, ...]
    plans: tuple[AdminEntry, ...]
    payment_methods: tuple[AdminEntry, ...]
    bots: tuple[AdminEntry, ...] = ()


async def _actor_id(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_user_id: int,
) -> UUID:
    # Telegram identity is global; no project-owned data is read here.
    async with session_factory() as session:
        user_id = await session.scalar(
            select(TelegramUser.id).where(TelegramUser.telegram_user_id == telegram_user_id)
        )
    if user_id is None:
        raise PermissionError("Нет доступа к управлению проектом")
    return user_id


async def _require_project_manager(
    session: AsyncSession,
    *,
    project_id: UUID,
    actor_user_id: UUID,
    lock: bool = False,
) -> Project:
    statement = select(Project).where(Project.id == project_id)
    if lock:
        statement = statement.with_for_update()
    project = await session.scalar(statement)
    if project is None or project.status != ProjectStatus.ACTIVE.value:
        raise PermissionError("Нет доступа к управлению проектом")

    if project.owner_user_id != actor_user_id:
        role = await session.scalar(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == actor_user_id,
            )
        )
        if role is None or not has_permission(
            ProjectMemberRole(role), Permission.PROJECT_MANAGE
        ):
            raise PermissionError("Нет доступа к управлению проектом")
    return project


async def load_admin_project(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    actor_telegram_user_id: int,
) -> AdminProjectSnapshot:
    actor_id = await _actor_id(session_factory, actor_telegram_user_id)
    try:
        async with tenant_session_scope(
            session_factory, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            project = await _require_project_manager(
                session, project_id=project_id, actor_user_id=actor_id
            )
            resources = (
                await session.scalars(
                    select(Resource)
                    .where(Resource.project_id == project_id)
                    .order_by(Resource.created_at, Resource.id)
                )
            ).all()
            plans = (
                await session.scalars(
                    select(Plan)
                    .where(Plan.project_id == project_id)
                    .order_by(Plan.created_at, Plan.id)
                )
            ).all()
            payment_methods = (
                await session.scalars(
                    select(PaymentMethod)
                    .where(PaymentMethod.project_id == project_id)
                    .order_by(PaymentMethod.sort_order, PaymentMethod.id)
                )
            ).all()
            bot_instances = (
                await session.scalars(
                    select(BotInstance)
                    .where(BotInstance.project_id == project_id)
                    .order_by(BotInstance.created_at, BotInstance.id)
                )
            ).all()
            return AdminProjectSnapshot(
                project_name=project.name,
                timezone=project.timezone,
                resources=tuple(
                    AdminEntry(
                        name=item.name,
                        status=item.status,
                        details=(
                            "Канал" if item.resource_type == "telegram_channel" else "Группа"
                        ),
                    )
                    for item in resources
                ),
                plans=tuple(
                    AdminEntry(name=item.name, status=item.status) for item in plans
                ),
                payment_methods=tuple(
                    AdminEntry(name=item.name, status=item.status)
                    for item in payment_methods
                ),
                bots=tuple(
                    AdminEntry(
                        name=f"@{item.username}",
                        status=item.status,
                        details="ожидает запуска" if item.status == "registered" else "",
                    )
                    for item in bot_instances
                ),
            )
    except TenantAccessDenied as exc:
        raise PermissionError("Нет доступа к управлению проектом") from exc


async def change_project_timezone(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    actor_telegram_user_id: int,
    timezone_name: str,
) -> str:
    timezone_name = timezone_name.strip()
    if len(timezone_name) > 64 or timezone_name not in available_timezones():
        raise ValueError("Неизвестный часовой пояс. Укажите IANA-зону, например Europe/Moscow")

    actor_id = await _actor_id(session_factory, actor_telegram_user_id)
    try:
        async with tenant_session_scope(
            session_factory, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            project = await _require_project_manager(
                session, project_id=project_id, actor_user_id=actor_id, lock=True
            )
            project.timezone = timezone_name
            await session.flush()
    except TenantAccessDenied as exc:
        raise PermissionError("Нет доступа к управлению проектом") from exc
    return timezone_name
