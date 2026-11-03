from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class PaymentProvider(StrEnum):
    MANUAL_SETTLEMENT = "manual_settlement"


class PaymentMethodStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class PaymentMethod(Base):
    __tablename__ = "payment_methods"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_payment_methods_id_project"),
        CheckConstraint(
            "provider IN ('manual_settlement')",
            name="ck_payment_methods_provider",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_payment_methods_status",
        ),
        CheckConstraint("sort_order >= 0", name="ck_payment_methods_sort_order"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(32),
        default=PaymentProvider.MANUAL_SETTLEMENT.value,
        server_default=PaymentProvider.MANUAL_SETTLEMENT.value,
        nullable=False,
    )
    instruction_template: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=PaymentMethodStatus.ACTIVE.value,
        server_default=PaymentMethodStatus.ACTIVE.value,
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PaymentMethodCurrency(Base):
    __tablename__ = "payment_method_currencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["payment_method_id", "project_id"],
            ["payment_methods.id", "payment_methods.project_id"],
            ondelete="CASCADE",
            name="fk_payment_method_currencies_method_project",
        ),
    )

    payment_method_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    currency_code: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("currency_assets.code", ondelete="RESTRICT"),
        primary_key=True,
    )
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
