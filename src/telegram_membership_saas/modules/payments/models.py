from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
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


class PaymentRequestStatus(StrEnum):
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_REVIEW = "waiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


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


class PaymentRequest(Base):
    __tablename__ = "payment_requests"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_payment_requests_id_project"),
        ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_payment_requests_customer_project",
        ),
        ForeignKeyConstraint(
            ["plan_price_id", "project_id", "plan_id", "currency_code"],
            [
                "plan_prices.id",
                "plan_prices.project_id",
                "plan_prices.plan_id",
                "plan_prices.currency_code",
            ],
            name="fk_payment_requests_price_snapshot",
        ),
        ForeignKeyConstraint(
            ["payment_method_id", "project_id"],
            ["payment_methods.id", "payment_methods.project_id"],
            name="fk_payment_requests_method_project",
        ),
        CheckConstraint("regular_amount > 0", name="ck_payment_requests_regular_positive"),
        CheckConstraint(
            "discount_amount >= 0",
            name="ck_payment_requests_discount_nonnegative",
        ),
        CheckConstraint("final_amount > 0", name="ck_payment_requests_final_positive"),
        CheckConstraint(
            "final_amount = regular_amount - discount_amount",
            name="ck_payment_requests_amount_math",
        ),
        CheckConstraint(
            "(duration_unit = 'lifetime' AND duration_value IS NULL) OR "
            "(duration_unit IN ('day', 'week', 'month', 'year') "
            "AND duration_value IS NOT NULL AND duration_value > 0)",
            name="ck_payment_requests_duration",
        ),
        CheckConstraint(
            "status IN ('waiting_confirmation', 'waiting_review', 'approved', 'rejected', "
            "'cancelled', 'expired')",
            name="ck_payment_requests_status",
        ),
        CheckConstraint(
            "(status = 'waiting_confirmation' AND submitted_at IS NULL "
            "AND reviewed_at IS NULL AND reviewed_by IS NULL) OR "
            "(status = 'waiting_review' AND submitted_at IS NOT NULL "
            "AND reviewed_at IS NULL AND reviewed_by IS NULL) OR "
            "(status IN ('approved', 'rejected') AND submitted_at IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL) OR "
            "(status IN ('cancelled', 'expired') AND submitted_at IS NULL "
            "AND reviewed_at IS NULL AND reviewed_by IS NULL)",
            name="ck_payment_requests_state_timestamps",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_price_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payment_method_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(16), nullable=False)
    regular_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(38, 18),
        default=Decimal("0"),
        server_default="0",
        nullable=False,
    )
    final_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    promotion_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    duration_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default=PaymentRequestStatus.WAITING_CONFIRMATION.value,
        server_default=PaymentRequestStatus.WAITING_CONFIRMATION.value,
        nullable=False,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_payments_id_project"),
        UniqueConstraint("payment_request_id", name="uq_payments_payment_request"),
        ForeignKeyConstraint(
            ["payment_request_id", "project_id"],
            ["payment_requests.id", "payment_requests.project_id"],
            ondelete="RESTRICT",
            name="fk_payments_request_project",
        ),
        ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            ondelete="RESTRICT",
            name="fk_payments_customer_project",
        ),
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["plans.id", "plans.project_id"],
            ondelete="RESTRICT",
            name="fk_payments_plan_project",
        ),
        ForeignKeyConstraint(
            ["plan_price_id", "project_id", "plan_id", "currency_code"],
            [
                "plan_prices.id",
                "plan_prices.project_id",
                "plan_prices.plan_id",
                "plan_prices.currency_code",
            ],
            ondelete="RESTRICT",
            name="fk_payments_price_snapshot",
        ),
        ForeignKeyConstraint(
            ["payment_method_id", "project_id"],
            ["payment_methods.id", "payment_methods.project_id"],
            ondelete="RESTRICT",
            name="fk_payments_method_project",
        ),
        CheckConstraint(
            "provider IN ('manual_settlement')",
            name="ck_payments_provider",
        ),
        CheckConstraint("regular_amount > 0", name="ck_payments_regular_positive"),
        CheckConstraint("discount_amount >= 0", name="ck_payments_discount_nonnegative"),
        CheckConstraint("final_amount > 0", name="ck_payments_final_positive"),
        CheckConstraint(
            "final_amount = regular_amount - discount_amount",
            name="ck_payments_amount_math",
        ),
        CheckConstraint(
            "(duration_unit = 'lifetime' AND duration_value IS NULL) OR "
            "(duration_unit IN ('day', 'week', 'month', 'year') "
            "AND duration_value IS NOT NULL AND duration_value > 0)",
            name="ck_payments_duration",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    payment_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_price_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payment_method_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(16), nullable=False)
    regular_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    final_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    promotion_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    duration_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
