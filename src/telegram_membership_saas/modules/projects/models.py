from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base

DEFAULT_PROJECT_TIMEZONE = "Europe/Moscow"
DEFAULT_PROJECT_LANGUAGE = "ru"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class ProjectMemberRole(StrEnum):
    ADMIN = "admin"
    PAYMENT_MANAGER = "payment_manager"
    SUPPORT = "support"
    VIEWER = "viewer"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'archived')",
            name="ck_projects_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64),
        default=DEFAULT_PROJECT_TIMEZONE,
        server_default=DEFAULT_PROJECT_TIMEZONE,
        nullable=False,
    )
    default_language: Mapped[str] = mapped_column(
        String(16),
        default=DEFAULT_PROJECT_LANGUAGE,
        server_default=DEFAULT_PROJECT_LANGUAGE,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default=ProjectStatus.ACTIVE.value,
        server_default=ProjectStatus.ACTIVE.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
        CheckConstraint(
            "role IN ('admin', 'payment_manager', 'support', 'viewer')",
            name="ck_project_members_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
