from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_subscriptions_id_project"),
        UniqueConstraint(
            "project_id",
            "customer_id",
            "plan_id",
            name="uq_subscriptions_project_customer_plan",
        ),
        ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_subscriptions_customer_project",
        ),
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["plans.id", "plans.project_id"],
            name="fk_subscriptions_plan_project",
        ),
        ForeignKeyConstraint(
            ["last_payment_id", "project_id"],
            ["payments.id", "payments.project_id"],
            name="fk_subscriptions_last_payment_project",
        ),
        CheckConstraint(
            "status IN ('active', 'expired', 'cancelled')",
            name="ck_subscriptions_status",
        ),
        CheckConstraint(
            "current_period_end IS NULL OR current_period_end > current_period_start",
            name="ck_subscriptions_period_order",
        ),
        CheckConstraint(
            "started_at <= current_period_start",
            name="ck_subscriptions_started_before_period",
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
    status: Mapped[str] = mapped_column(
        String(16),
        default=SubscriptionStatus.ACTIVE.value,
        server_default=SubscriptionStatus.ACTIVE.value,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_payment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
