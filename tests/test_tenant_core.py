from sqlalchemy import UniqueConstraint

from telegram_membership_saas.db_base import Base
from telegram_membership_saas.model_registry import Project, ProjectMember, TelegramUser
from telegram_membership_saas.modules.projects import (
    ALL_PERMISSIONS,
    DEFAULT_PROJECT_LANGUAGE,
    DEFAULT_PROJECT_TIMEZONE,
    Permission,
    ProjectMemberRole,
    has_permission,
    permissions_for_role,
)


def test_tenant_core_tables_registered() -> None:
    assert {"telegram_users", "projects", "project_members"} <= set(Base.metadata.tables)
    assert TelegramUser.__tablename__ == "telegram_users"
    assert Project.__tablename__ == "projects"
    assert ProjectMember.__tablename__ == "project_members"


def test_project_defaults() -> None:
    table = Project.__table__
    assert table.c.timezone.default.arg == DEFAULT_PROJECT_TIMEZONE
    assert table.c.timezone.server_default.arg == DEFAULT_PROJECT_TIMEZONE
    assert table.c.default_language.default.arg == DEFAULT_PROJECT_LANGUAGE


def test_project_member_is_unique_per_project_and_user() -> None:
    constraints = {
        constraint.name
        for constraint in ProjectMember.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_project_members_project_user" in constraints


def test_owner_has_all_permissions() -> None:
    assert permissions_for_role(None, is_owner=True) == ALL_PERMISSIONS


def test_payment_manager_permissions_are_limited() -> None:
    role = ProjectMemberRole.PAYMENT_MANAGER
    assert has_permission(role, Permission.PAYMENT_APPROVE)
    assert has_permission(role, Permission.PAYMENT_REJECT)
    assert not has_permission(role, Permission.PROJECT_MANAGE)
    assert not has_permission(role, Permission.ADMINS_MANAGE)


def test_unknown_membership_is_denied_by_default() -> None:
    assert permissions_for_role(None) == frozenset()
    assert not has_permission(None, Permission.PROJECT_VIEW)
