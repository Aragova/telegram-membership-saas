from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from telegram_membership_saas.db_base import Base
from telegram_membership_saas.model_registry import PaymentMethod, PaymentMethodCurrency
from telegram_membership_saas.modules.payments import PaymentProvider


def test_payment_method_tables_registered() -> None:
    assert {"payment_methods", "payment_method_currencies"} <= set(Base.metadata.tables)
    assert PaymentMethod.__tablename__ == "payment_methods"
    assert PaymentMethodCurrency.__tablename__ == "payment_method_currencies"


def test_manual_settlement_is_provider_not_currency() -> None:
    assert PaymentProvider.MANUAL_SETTLEMENT.value == "manual_settlement"
    assert "currency_code" not in PaymentMethod.__table__.c


def test_payment_method_has_project_composite_identity() -> None:
    constraints = {
        constraint.name
        for constraint in PaymentMethod.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_payment_methods_id_project" in constraints


def test_payment_method_currency_uses_composite_tenant_fk() -> None:
    constraints = {
        constraint.name
        for constraint in PaymentMethodCurrency.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert "fk_payment_method_currencies_method_project" in constraints


def test_payment_method_database_checks_exist() -> None:
    checks = {
        constraint.name
        for constraint in PaymentMethod.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "ck_payment_methods_provider",
        "ck_payment_methods_status",
        "ck_payment_methods_sort_order",
    } <= checks
