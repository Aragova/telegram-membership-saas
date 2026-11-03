from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


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
