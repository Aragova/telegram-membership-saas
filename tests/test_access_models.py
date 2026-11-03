from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from telegram_membership_saas.db_base import Base
from telegram_membership_saas.model_registry import AccessAction, AccessState, EffectiveAccess
from telegram_membership_saas.modules.access import (
    AccessActionStatus,
    AccessActionType,
    ActualAccessState,
)


def test_access_tables_registered() -> None:
    assert {"effective_access", "access_states", "access_actions"} <= set(Base.metadata.tables)


def test_effective_access_is_tenant_safe() -> None:
    constraints = {
        constraint.name
        for constraint in EffectiveAccess.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert {
        "fk_effective_access_customer_project",
        "fk_effective_access_resource_project",
    } <= constraints

    unique_constraints = {
        constraint.name
        for constraint in EffectiveAccess.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_effective_access_target" in unique_constraints


def test_access_state_and_action_checks_are_explicit() -> None:
    state_checks = {
        constraint.name
        for constraint in AccessState.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    action_checks = {
        constraint.name
        for constraint in AccessAction.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_access_states_actual_state" in state_checks
    assert {
        "ck_access_actions_type",
        "ck_access_actions_status",
        "ck_access_actions_attempt_count",
    } <= action_checks


def test_access_enums_are_explicit() -> None:
    assert {state.value for state in ActualAccessState} == {
        "unknown",
        "granted",
        "revoked",
    }
    assert {action.value for action in AccessActionType} == {"grant", "revoke"}
    assert {status.value for status in AccessActionStatus} == {
        "pending",
        "processing",
        "succeeded",
        "failed_retryable",
        "failed_permanent",
        "superseded",
    }
