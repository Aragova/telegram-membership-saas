import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.projects import Project, ProjectMember, ProjectMemberRole
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for PostgreSQL RLS integration tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_global_users(session_factory) -> tuple[UUID, UUID]:
    async with session_factory() as session:
        async with session.begin():
            user_a = TelegramUser(telegram_user_id=_random_telegram_id(), username="tenant_a")
            user_b = TelegramUser(telegram_user_id=_random_telegram_id(), username="tenant_b")
            session.add_all([user_a, user_b])
            await session.flush()
            return user_a.id, user_b.id


async def _create_project(session_factory, user_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(session_factory, TenantContext(user_id=user_id)) as session:
        project = Project(owner_user_id=user_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def _add_member(
    session_factory,
    *,
    owner_user_id: UUID,
    project_id: UUID,
    member_user_id: UUID,
) -> None:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=owner_user_id, project_id=project_id),
    ) as session:
        session.add(
            ProjectMember(
                project_id=project_id,
                user_id=member_user_id,
                role=ProjectMemberRole.VIEWER.value,
            )
        )


async def test_rls_blocks_cross_project_access_and_narrows_active_scope() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        user_a_id, user_b_id = await _create_global_users(session_factory)
        project_a_id = await _create_project(session_factory, user_a_id, "Project A")
        project_b_id = await _create_project(session_factory, user_b_id, "Project B")
        project_c_id = await _create_project(session_factory, user_b_id, "Project C")

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_a_id),
        ) as session:
            visible = set((await session.scalars(select(Project.id))).all())
            assert project_a_id in visible
            assert project_b_id not in visible
            assert project_c_id not in visible

        with pytest.raises(TenantAccessDenied):
            async with tenant_session_scope(
                session_factory,
                TenantContext(user_id=user_a_id, project_id=project_b_id),
            ):
                pass

        await _add_member(
            session_factory,
            owner_user_id=user_b_id,
            project_id=project_b_id,
            member_user_id=user_a_id,
        )
        await _add_member(
            session_factory,
            owner_user_id=user_b_id,
            project_id=project_c_id,
            member_user_id=user_a_id,
        )

        # Discovery mode can see every project the user owns or belongs to.
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_a_id),
        ) as session:
            visible = set((await session.scalars(select(Project.id))).all())
            assert {project_a_id, project_b_id, project_c_id}.issubset(visible)

            membership_projects = set(
                (await session.scalars(select(ProjectMember.project_id))).all()
            )
            assert project_b_id in membership_projects
            assert project_c_id in membership_projects

        # Once project B is activated, both project and membership reads must be
        # narrowed to B; A's own project and the separate C membership must not leak.
        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_a_id, project_id=project_b_id),
        ) as session:
            scoped_projects = set((await session.scalars(select(Project.id))).all())
            assert scoped_projects == {project_b_id}

            scoped_memberships = set(
                (await session.scalars(select(ProjectMember.project_id))).all()
            )
            assert scoped_memberships == {project_b_id}
    finally:
        await engine.dispose()
