from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
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


class ActualAccessState(StrEnum):
    UNKNOWN = "unknown"
    GRANTED = "granted"
    REVOKED = "revoked"


class EffectiveAccess(Base):
    __tablename__ = "effective_access"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_effective_access_id_project"),
        UniqueConstraint(
            "project_id",
            "customer_id",
            "resource_id",
            name="uq_effective_access_target",
        ),
        ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_effective_access_customer_project",
        ),
        ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            name="fk_effective_access_resource_project",
        ),
        CheckConstraint(
            "active_grant_count >= 0",
            name="ck_effective_access_grant_count",
        ),
        CheckConstraint(
            "(has_access AND active_grant_count > 0) OR "
            "(NOT has_access AND active_grant_count = 0)",
            name="ck_effective_access_consistency",
        ),
        CheckConstraint("revision >= 0", name="ck_effective_access_revision"),
        Index(
            "ix_effective_access_next_change",
            "project_id",
            "next_change_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    has_access: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    active_grant_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    next_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AccessState(Base):
    __tablename__ = "access_states"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_access_states_id_project"),
        UniqueConstraint(
            "project_id",
            "customer_id",
            "resource_id",
            name="uq_access_states_target",
        ),
        ForeignKeyConstraint(
            ["project_id", "customer_id", "resource_id"],
            [
                "effective_access.project_id",
                "effective_access.customer_id",
                "effective_access.resource_id",
            ],
            name="fk_access_states_effective_access_target",
            ondelete="CASCADE",
        ),
        CheckConstraint("desired_revision >= 0", name="ck_access_states_desired_revision"),
        CheckConstraint(
            "actual_state IN ('unknown', 'granted', 'revoked')",
            name="ck_access_states_actual_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    desired_access: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    desired_revision: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    actual_state: Mapped[str] = mapped_column(
        String(16),
        default=ActualAccessState.UNKNOWN.value,
        server_default=ActualAccessState.UNKNOWN.value,
        nullable=False,
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
