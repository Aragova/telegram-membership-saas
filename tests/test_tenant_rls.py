import os
from uuid import UUID

import pytest
from sqlalchemy import select, text

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.projects import Project, ProjectMember, ProjectMemberRole
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL is required for PostgreSQL RLS integration tests",
)


async def _reset_database(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("TRUNCATE project_members, projects, telegram_users RESTART IDENTITY CASCADE")
            )


async def _create_global_users(session_factory) -> tuple[UUID, UUID]:
    async with session_factory() as session:
        async with session.begin():
            user_a = TelegramUser(telegram_user_id=100001, username="tenant_a")
            user_b = TelegramUser(telegram_user_id=100002, username="tenant_b")
            session.add_all([user_a, user_b])
            await session.flush()
            return user_a.id, user_b.id


async def _create_project(session_factory, user_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(session_factory, TenantContext(user_id=user_id)) as session:
        project = Project(owner_user_id=user_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def test_rls_blocks_cross_project_access_and_allows_membership() -> None:
    settings = Settings(database_url=os.environ["DATABASE_URL"])
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        await _reset_database(session_factory)
        user_a_id, user_b_id = await _create_global_users(session_factory)
        project_a_id = await _create_project(session_factory, user_a_id, "Project A")
        project_b_id = await _create_project(session_factory, user_b_id, "Project B")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_a_id),
        ) as session:
            visible = set((await session.scalars(select(Project.id))).all())
            assert visible == {project_a_id}

        with pytest.raises(TenantAccessDenied):
            async with tenant_session_scope(
                session_factory,
                TenantContext(user_id=user_a_id, project_id=project_b_id),
            ):
                pass

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_b_id, project_id=project_b_id),
        ) as session:
            session.add(
                ProjectMember(
                    project_id=project_b_id,
                    user_id=user_a_id,
                    role=ProjectMemberRole.VIEWER.value,
                )
            )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_a_id),
        ) as session:
            visible = set((await session.scalars(select(Project.id))).all())
            assert visible == {project_a_id, project_b_id}

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_a_id, project_id=project_b_id),
        ) as session:
            visible_members = set(
                (await session.scalars(select(ProjectMember.user_id))).all()
            )
            assert user_a_id in visible_members
    finally:
        await engine.dispose()
