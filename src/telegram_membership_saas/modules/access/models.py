from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint, Uuid, func
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
            name="uq_effective_access_customer_resource",
        ),
        ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            ondelete="CASCADE",
            name="fk_effective_access_customer_project",
        ),
        ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            ondelete="CASCADE",
            name="fk_effective_access_resource_project",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    desired_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
