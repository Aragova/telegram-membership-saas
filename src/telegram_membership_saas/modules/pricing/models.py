from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class CurrencyAssetType(StrEnum):
    FIAT = "fiat"
    CRYPTO = "crypto"


class CurrencyAssetStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class PlanPriceStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class CurrencyAsset(Base):
    __tablename__ = "currency_assets"
    __table_args__ = (
        CheckConstraint("asset_type IN ('fiat', 'crypto')", name="ck_currency_assets_type"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_currency_assets_status"),
        CheckConstraint("precision >= 0 AND precision <= 18", name="ck_currency_assets_precision"),
    )

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    precision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=CurrencyAssetStatus.ACTIVE.value,
        server_default=CurrencyAssetStatus.ACTIVE.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PlanPrice(Base):
    __tablename__ = "plan_prices"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_plan_prices_id_project"),
        UniqueConstraint(
            "id",
            "project_id",
            "plan_id",
            "currency_code",
            name="uq_plan_prices_request_identity",
        ),
        UniqueConstraint(
            "plan_id",
            "currency_code",
            "version",
            name="uq_plan_prices_plan_currency_version",
        ),
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["plans.id", "plans.project_id"],
            ondelete="CASCADE",
            name="fk_plan_prices_plan_project",
        ),
        CheckConstraint("amount > 0", name="ck_plan_prices_amount_positive"),
        CheckConstraint("version >= 1", name="ck_plan_prices_version_positive"),
        CheckConstraint(
            "status IN ('active', 'retired')",
            name="ck_plan_prices_status",
        ),
        CheckConstraint(
            "(status = 'active' AND active_until IS NULL) OR "
            "(status = 'retired' AND active_until IS NOT NULL)",
            name="ck_plan_prices_active_window",
        ),
        Index(
            "uq_plan_prices_active_pair",
            "plan_id",
            "currency_code",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("currency_assets.code", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=PlanPriceStatus.ACTIVE.value,
        server_default=PlanPriceStatus.ACTIVE.value,
        nullable=False,
    )
    active_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
