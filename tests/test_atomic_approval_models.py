from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from telegram_membership_saas.db_base import Base
from telegram_membership_saas.model_registry import (
    EntitlementGrant,
    OutboxEvent,
    Payment,
    Subscription,
)
from telegram_membership_saas.modules.entitlements import EntitlementSource
from telegram_membership_saas.modules.outbox import OutboxEventType


def _constraint_names(model, constraint_type):
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, constraint_type)
    }


def test_atomic_approval_tables_registered() -> None:
    assert {
        "payments",
        "subscriptions",
        "entitlement_grants",
        "outbox_events",
    } <= set(Base.metadata.tables)


def test_payment_has_exactly_once_request_constraint_and_tenant_fks() -> None:
    unique_names = _constraint_names(Payment, UniqueConstraint)
    fk_names = _constraint_names(Payment, ForeignKeyConstraint)

    assert "uq_payments_payment_request" in unique_names
    assert {
        "fk_payments_request_project",
        "fk_payments_customer_project",
        "fk_payments_plan_project",
        "fk_payments_price_snapshot",
        "fk_payments_method_project",
    } <= fk_names


def test_subscription_is_unique_per_project_customer_plan() -> None:
    unique_names = _constraint_names(Subscription, UniqueConstraint)
    fk_names = _constraint_names(Subscription, ForeignKeyConstraint)

    assert "uq_subscriptions_project_customer_plan" in unique_names
    assert {
        "fk_subscriptions_customer_project",
        "fk_subscriptions_plan_project",
        "fk_subscriptions_last_payment_project",
    } <= fk_names


def test_entitlement_grant_has_polymorphic_source_and_resource_identity() -> None:
    unique_names = _constraint_names(EntitlementGrant, UniqueConstraint)
    fk_names = _constraint_names(EntitlementGrant, ForeignKeyConstraint)

    assert "uq_entitlement_grants_source_resource" in unique_names
    assert {
        "fk_entitlement_grants_customer_project",
        "fk_entitlement_grants_resource_project",
    } <= fk_names
    assert EntitlementSource.PAYMENT.value == "payment"


def test_outbox_has_dedupe_constraint() -> None:
    unique_names = _constraint_names(OutboxEvent, UniqueConstraint)
    check_names = _constraint_names(OutboxEvent, CheckConstraint)

    assert "uq_outbox_events_project_dedupe" in unique_names
    assert "ck_outbox_events_status" in check_names
    assert OutboxEventType.ACCESS_RECALCULATE.value == "access_recalculate"
