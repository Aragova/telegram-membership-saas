from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from telegram_membership_saas.db_base import Base
from telegram_membership_saas.model_registry import PaymentRequest, ProjectCustomer
from telegram_membership_saas.modules.payments import PaymentRequestStatus


def test_payment_request_tables_registered() -> None:
    assert {"project_customers", "payment_requests"} <= set(Base.metadata.tables)
    assert ProjectCustomer.__tablename__ == "project_customers"
    assert PaymentRequest.__tablename__ == "payment_requests"


def test_project_customer_has_tenant_identity() -> None:
    constraints = {
        constraint.name
        for constraint in ProjectCustomer.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert {
        "uq_project_customers_id_project",
        "uq_project_customers_project_user",
    } <= constraints


def test_payment_request_uses_tenant_safe_snapshot_foreign_keys() -> None:
    constraints = {
        constraint.name
        for constraint in PaymentRequest.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert {
        "fk_payment_requests_customer_project",
        "fk_payment_requests_price_snapshot",
        "fk_payment_requests_method_project",
    } <= constraints


def test_payment_request_database_checks_exist() -> None:
    checks = {
        constraint.name
        for constraint in PaymentRequest.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "ck_payment_requests_regular_positive",
        "ck_payment_requests_discount_nonnegative",
        "ck_payment_requests_final_positive",
        "ck_payment_requests_amount_math",
        "ck_payment_requests_status",
        "ck_payment_requests_state_timestamps",
    } <= checks


def test_payment_request_has_no_proof_storage_columns() -> None:
    columns = set(PaymentRequest.__table__.c.keys())
    forbidden = {
        "proof_file_id",
        "proof_url",
        "proof_hash",
        "receipt_file_id",
        "receipt_url",
        "receipt_hash",
    }
    assert columns.isdisjoint(forbidden)


def test_payment_request_statuses_are_explicit() -> None:
    assert {status.value for status in PaymentRequestStatus} == {
        "waiting_confirmation",
        "waiting_review",
        "approved",
        "rejected",
        "cancelled",
        "expired",
    }
