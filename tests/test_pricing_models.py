from sqlalchemy import Index, UniqueConstraint

from telegram_membership_saas.db_base import Base
from telegram_membership_saas.model_registry import CurrencyAsset, Plan, PlanPrice
from telegram_membership_saas.modules.pricing import PlanPriceStatus


def test_pricing_tables_registered() -> None:
    assert {"currency_assets", "plan_prices"} <= set(Base.metadata.tables)
    assert CurrencyAsset.__tablename__ == "currency_assets"
    assert PlanPrice.__tablename__ == "plan_prices"


def test_plan_has_no_primary_currency_or_base_price() -> None:
    columns = set(Plan.__table__.c.keys())
    assert "primary_currency" not in columns
    assert "base_price" not in columns
    assert "currency_code" not in columns


def test_plan_price_versions_are_unique() -> None:
    constraints = {
        constraint.name
        for constraint in PlanPrice.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_plan_prices_plan_currency_version" in constraints


def test_only_one_active_price_per_plan_currency_is_indexed() -> None:
    indexes = {index.name: index for index in PlanPrice.__table__.indexes if isinstance(index, Index)}
    index = indexes["uq_plan_prices_active_pair"]
    assert index.unique is True
    assert "status = 'active'" in str(index.dialect_options["postgresql"]["where"])


def test_new_price_defaults_active() -> None:
    assert PlanPrice.__table__.c.status.default.arg == PlanPriceStatus.ACTIVE.value
    assert PlanPrice.__table__.c.status.server_default.arg == PlanPriceStatus.ACTIVE.value
