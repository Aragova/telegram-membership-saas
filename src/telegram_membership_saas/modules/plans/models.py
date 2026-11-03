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
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class DurationUnit(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    LIFETIME = "lifetime"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_plans_id_project"),
        CheckConstraint(
            "duration_unit IN ('day', 'week', 'month', 'year', 'lifetime')",
            name="ck_plans_duration_unit",
        ),
        CheckConstraint(
            "(duration_unit = 'lifetime' AND duration_value IS NULL) OR "
            "(duration_unit <> 'lifetime' AND duration_value IS NOT NULL AND duration_value > 0)",
            name="ck_plans_duration_value",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_plans_status",
        ),
        CheckConstraint("sort_order >= 0", name="ck_plans_sort_order"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    duration_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default=PlanStatus.DRAFT.value,
        server_default=PlanStatus.DRAFT.value,
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PlanResource(Base):
    __tablename__ = "plan_resources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["plans.id", "plans.project_id"],
            ondelete="CASCADE",
            name="fk_plan_resources_plan_project",
        ),
        ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            ondelete="CASCADE",
            name="fk_plan_resources_resource_project",
        ),
    )

    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
