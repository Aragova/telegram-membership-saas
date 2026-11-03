from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from telegram_membership_saas.db_base import Base
from telegram_membership_saas.model_registry import EffectiveAccess, OwnerServiceAccount


def test_effective_access_and_owner_service_tables_registered() -> None:
    assert {"effective_access", "owner_service_accounts"} <= set(Base.metadata.tables)


def test_effective_access_is_tenant_safe() -> None:
    foreign_keys = {
        constraint.name
        for constraint in EffectiveAccess.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    uniques = {
        constraint.name
        for constraint in EffectiveAccess.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert {
        "fk_effective_access_customer_project",
        "fk_effective_access_resource_project",
    } <= foreign_keys
    assert "uq_effective_access_customer_resource" in uniques


def test_owner_service_prevents_duplicate_identity_assignment() -> None:
    uniques = {
        constraint.name
        for constraint in OwnerServiceAccount.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_owner_service_accounts_owner_service_user" in uniques
