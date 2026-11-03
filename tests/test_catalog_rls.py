import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.plans import DurationUnit, Plan, PlanResource
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="TMS_DATABASE_URL is required for PostgreSQL catalog integration tests",
)


def _random_telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _create_user(session_factory, username: str) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            user = TelegramUser(telegram_user_id=_random_telegram_id(), username=username)
            session.add(user)
            await session.flush()
            return user.id


async def _create_project(session_factory, user_id: UUID, name: str) -> UUID:
    async with tenant_session_scope(session_factory, TenantContext(user_id=user_id)) as session:
        project = Project(owner_user_id=user_id, name=name)
        session.add(project)
        await session.flush()
        return project.id


async def _create_catalog(
    session_factory,
    *,
    user_id: UUID,
    project_id: UUID,
    suffix: str,
) -> tuple[UUID, UUID]:
    async with tenant_session_scope(
        session_factory,
        TenantContext(user_id=user_id, project_id=project_id),
    ) as session:
        resource = Resource(
            project_id=project_id,
            resource_type=ResourceType.TELEGRAM_CHANNEL.value,
            external_id=f"-100{uuid4().int % 10_000_000_000}",
            name=f"Channel {suffix}",
        )
        plan = Plan(
            project_id=project_id,
            name=f"Plan {suffix}",
            duration_value=1,
            duration_unit=DurationUnit.MONTH.value,
        )
        session.add_all([resource, plan])
        await session.flush()
        session.add(
            PlanResource(
                project_id=project_id,
                plan_id=plan.id,
                resource_id=resource.id,
            )
        )
        await session.flush()
        return resource.id, plan.id


async def test_catalog_rls_and_cross_project_relation_guard() -> None:
    settings = Settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)

    try:
        user_a_id = await _create_user(session_factory, f"catalog_a_{uuid4().hex[:8]}")
        user_b_id = await _create_user(session_factory, f"catalog_b_{uuid4().hex[:8]}")
        project_a_id = await _create_project(session_factory, user_a_id, "Catalog Project A")
        project_b_id = await _create_project(session_factory, user_b_id, "Catalog Project B")

        resource_a_id, plan_a_id = await _create_catalog(
            session_factory,
            user_id=user_a_id,
            project_id=project_a_id,
            suffix="A",
        )
        resource_b_id, _ = await _create_catalog(
            session_factory,
            user_id=user_b_id,
            project_id=project_b_id,
            suffix="B",
        )

        async with tenant_session_scope(
            session_factory,
            TenantContext(user_id=user_a_id, project_id=project_a_id),
        ) as session:
            assert set((await session.scalars(select(Resource.id))).all()) == {resource_a_id}
            assert set((await session.scalars(select(Plan.id))).all()) == {plan_a_id}
            links = (await session.scalars(select(PlanResource.resource_id))).all()
            assert links == [resource_a_id]

        with pytest.raises(IntegrityError):
            async with tenant_session_scope(
                session_factory,
                TenantContext(user_id=user_a_id, project_id=project_a_id),
            ) as session:
                session.add(
                    PlanResource(
                        project_id=project_a_id,
                        plan_id=plan_a_id,
                        resource_id=resource_b_id,
                    )
                )
                await session.flush()
    finally:
        await engine.dispose()
