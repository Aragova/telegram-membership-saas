from enum import StrEnum

from telegram_membership_saas.modules.projects.models import ProjectMemberRole


class Permission(StrEnum):
    PROJECT_VIEW = "project.view"
    PROJECT_MANAGE = "project.manage"
    PLAN_VIEW = "plan.view"
    PLAN_EDIT = "plan.edit"
    PAYMENT_VIEW = "payment.view"
    PAYMENT_APPROVE = "payment.approve"
    PAYMENT_REJECT = "payment.reject"
    SUBSCRIBER_VIEW = "subscriber.view"
    SUBSCRIBER_GRANT = "subscriber.grant"
    SUBSCRIBER_REVOKE = "subscriber.revoke"
    ANALYTICS_VIEW = "analytics.view"
    ADMINS_MANAGE = "admins.manage"


ALL_PERMISSIONS = frozenset(Permission)

_ROLE_PERMISSIONS: dict[ProjectMemberRole, frozenset[Permission]] = {
    ProjectMemberRole.ADMIN: frozenset(
        {
            Permission.PROJECT_VIEW,
            Permission.PROJECT_MANAGE,
            Permission.PLAN_VIEW,
            Permission.PLAN_EDIT,
            Permission.PAYMENT_VIEW,
            Permission.PAYMENT_APPROVE,
            Permission.PAYMENT_REJECT,
            Permission.SUBSCRIBER_VIEW,
            Permission.SUBSCRIBER_GRANT,
            Permission.SUBSCRIBER_REVOKE,
            Permission.ANALYTICS_VIEW,
        }
    ),
    ProjectMemberRole.PAYMENT_MANAGER: frozenset(
        {
            Permission.PROJECT_VIEW,
            Permission.PAYMENT_VIEW,
            Permission.PAYMENT_APPROVE,
            Permission.PAYMENT_REJECT,
            Permission.SUBSCRIBER_VIEW,
        }
    ),
    ProjectMemberRole.SUPPORT: frozenset(
        {
            Permission.PROJECT_VIEW,
            Permission.SUBSCRIBER_VIEW,
        }
    ),
    ProjectMemberRole.VIEWER: frozenset(
        {
            Permission.PROJECT_VIEW,
            Permission.PLAN_VIEW,
            Permission.PAYMENT_VIEW,
            Permission.SUBSCRIBER_VIEW,
            Permission.ANALYTICS_VIEW,
        }
    ),
}


def permissions_for_role(
    role: ProjectMemberRole | None,
    *,
    is_owner: bool = False,
) -> frozenset[Permission]:
    if is_owner:
        return ALL_PERMISSIONS
    if role is None:
        return frozenset()
    return _ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(
    role: ProjectMemberRole | None,
    permission: Permission,
    *,
    is_owner: bool = False,
) -> bool:
    return permission in permissions_for_role(role, is_owner=is_owner)
