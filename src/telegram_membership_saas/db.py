from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.tenant_context import (
    TenantAccessDenied,
    TenantContext,
    get_tenant_context,
)


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    current = settings or get_settings()
    return create_async_engine(current.database_url, pool_pre_ping=True)


def build_lease_engine(settings: Settings | None = None) -> AsyncEngine:
    """Keep long-lived advisory leases off the operational database pool.

    Each running project holds a PostgreSQL session for its whole lifetime.
    The dedicated NullPool makes that session independent of the pool used
    for payments, Telegram updates, expiry/revocations and heartbeat writes.
    The supervisor also caps concurrent projects to preserve database slots.
    """
    current = settings or get_settings()
    return create_async_engine(
        current.database_url,
        poolclass=NullPool,
        pool_pre_ping=True,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Unscoped session for global tables, migrations, and maintenance code only."""
    async with session_factory() as session:
        yield session


async def _set_local_setting(session: AsyncSession, key: str, value: str) -> None:
    await session.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": key, "value": value},
    )


async def _bind_user_context(session: AsyncSession, user_id: UUID) -> None:
    await _set_local_setting(session, "app.current_user_id", str(user_id))
    await _set_local_setting(session, "app.current_project_id", "")


async def _activate_project_scope(session: AsyncSession, project_id: UUID) -> None:
    accessible_project = await session.scalar(select(Project.id).where(Project.id == project_id))
    if accessible_project is None:
        raise TenantAccessDenied(f"Project {project_id} is not accessible to the current user")
    await _set_local_setting(session, "app.current_project_id", str(project_id))


async def apply_tenant_context(
    session: AsyncSession,
    context: TenantContext | None = None,
) -> TenantContext:
    current = context or get_tenant_context()
    await _bind_user_context(session, current.user_id)
    if current.project_id is not None:
        await _activate_project_scope(session, current.project_id)
    return current


async def apply_system_project_context(session: AsyncSession, project_id: UUID) -> None:
    """Bind trusted background work to one project without impersonating an app user."""
    await _set_local_setting(session, "app.current_user_id", "")
    await _set_local_setting(session, "app.current_project_id", str(project_id))


@asynccontextmanager
async def tenant_session_scope(
    session_factory: async_sessionmaker[AsyncSession],
    context: TenantContext | None = None,
) -> AsyncIterator[AsyncSession]:
    """Open one transaction with PostgreSQL RLS context bound for its full lifetime."""
    async with session_factory() as session:
        async with session.begin():
            await apply_tenant_context(session, context)
            yield session


@asynccontextmanager
async def system_project_session_scope(
    session_factory: async_sessionmaker[AsyncSession],
    project_id: UUID,
) -> AsyncIterator[AsyncSession]:
    """Trusted worker/Telegram ingress scope; keeps RLS narrowed to exactly one project."""
    async with session_factory() as session:
        async with session.begin():
            await apply_system_project_context(session, project_id)
            yield session
