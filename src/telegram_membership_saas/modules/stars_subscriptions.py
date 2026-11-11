"""Native Telegram Stars checkout order; paid source and grant journal."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class SubscriptionStarOrder(Base):
    __tablename__ = "subscription_star_orders"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_subscription_star_order_project"),
        Index(
            "ix_subscription_star_order_paid", "paid_at", "id",
            postgresql_where=text("status='paid'"),
        ),
        Index(
            "ix_subscription_star_order_customer",
            "project_id", "customer_id", "created_at",
        ),
        UniqueConstraint("invoice_payload", name="uq_subscription_star_payload"),
        UniqueConstraint(
            "telegram_bot_id", "telegram_charge_id",
            name="uq_subscription_star_bot_charge",
        ),
        ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["plans.id", "plans.project_id"], ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["plan_price_id", "project_id", "plan_id", "currency_code"],
            ["plan_prices.id", "plan_prices.project_id",
             "plan_prices.plan_id", "plan_prices.currency_code"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("currency_code='XTR'", name="ck_subscription_star_currency"),
        CheckConstraint(
            "stars_amount BETWEEN 1 AND 250000", name="ck_subscription_star_amount",
        ),
        CheckConstraint(
            "(status='pending' AND telegram_charge_id IS NULL AND paid_at IS NULL)"
            " OR (status='paid' AND telegram_charge_id IS NOT NULL "
            "AND paid_at IS NOT NULL)",
            name="ck_subscription_star_state",
        ),
        CheckConstraint(
            "(duration_unit='lifetime' AND duration_value IS NULL) OR "
            "(duration_unit IN ('day','week','month','year') "
            "AND duration_value IS NOT NULL AND duration_value>0)",
            name="ck_subscription_star_duration",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False,
    )
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_price_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(16), nullable=False, default="XTR",
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    invoice_payload: Mapped[str] = mapped_column(String(128), nullable=False)
    stars_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_name_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    duration_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending",
    )
    telegram_charge_id: Mapped[str | None] = mapped_column(String(256))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    precheckout_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(),
        nullable=False,
    )


class SubscriptionStarRefund(Base):
    """One authenticated, immutable Telegram refund of a Stars subscription."""

    __tablename__ = "subscription_star_refunds"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id", "project_id"],
            ["subscription_star_orders.id", "subscription_star_orders.project_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("order_id", name="uq_subscription_star_refund_order"),
        UniqueConstraint(
            "telegram_bot_id", "telegram_charge_id",
            name="uq_subscription_star_refund_charge",
        ),
        CheckConstraint(
            "stars_amount BETWEEN 1 AND 250000",
            name="ck_subscription_star_refund_amount",
        ),
    )
    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    order_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    invoice_payload: Mapped[str] = mapped_column(String(128), nullable=False)
    telegram_charge_id: Mapped[str] = mapped_column(String(256), nullable=False)
    stars_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    refunded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.clock_timestamp(),
    )
