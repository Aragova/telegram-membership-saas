from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class ResourceType(StrEnum):
    TELEGRAM_CHANNEL = "telegram_channel"
    TELEGRAM_GROUP = "telegram_group"


class ResourceStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_resources_id_project"),
        UniqueConstraint(
            "project_id",
            "resource_type",
            "external_id",
            name="uq_resources_project_type_external",
        ),
        CheckConstraint(
            "resource_type IN ('telegram_channel', 'telegram_group')",
            name="ck_resources_type",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled', 'archived')",
            name="ck_resources_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default=ResourceStatus.ACTIVE.value,
        server_default=ResourceStatus.ACTIVE.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
