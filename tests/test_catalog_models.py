from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from telegram_membership_saas.db_base import Base
from telegram_membership_saas.model_registry import Plan, PlanResource, Resource
from telegram_membership_saas.modules.plans import DurationUnit, PlanStatus
from telegram_membership_saas.modules.projects import Permission, ProjectMemberRole, has_permission
from telegram_membership_saas.modules.resources import ResourceStatus, ResourceType


def test_catalog_tables_registered() -> None:
    assert {"resources", "plans", "plan_resources"} <= set(Base.metadata.tables)
    assert Resource.__tablename__ == "resources"
    assert Plan.__tablename__ == "plans"
    assert PlanResource.__tablename__ == "plan_resources"


def test_catalog_enum_contracts() -> None:
    assert ResourceType.TELEGRAM_CHANNEL.value == "telegram_channel"
    assert ResourceStatus.ACTIVE.value == "active"
    assert DurationUnit.MONTH.value == "month"
    assert DurationUnit.LIFETIME.value == "lifetime"
    assert PlanStatus.DRAFT.value == "draft"


def test_plan_resource_enforces_same_project_relations() -> None:
    names = {
        constraint.name
        for constraint in PlanResource.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert "fk_plan_resources_plan_project" in names
    assert "fk_plan_resources_resource_project" in names

    plan_unique_names = {
        constraint.name
        for constraint in Plan.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    resource_unique_names = {
        constraint.name
        for constraint in Resource.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_plans_id_project" in plan_unique_names
    assert "uq_resources_id_project" in resource_unique_names


def test_resource_permissions_are_role_scoped() -> None:
    assert has_permission(ProjectMemberRole.ADMIN, Permission.RESOURCE_MANAGE)
    assert has_permission(ProjectMemberRole.VIEWER, Permission.RESOURCE_VIEW)
    assert not has_permission(ProjectMemberRole.VIEWER, Permission.RESOURCE_MANAGE)
    assert not has_permission(ProjectMemberRole.PAYMENT_MANAGER, Permission.RESOURCE_VIEW)
