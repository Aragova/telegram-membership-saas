"""Owner-wide reusable config snapshots, isolated from sales/finance."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class OwnerNetworkProfile(Base):
    __tablename__ = "owner_network_profiles"
    __table_args__ = (
        UniqueConstraint("owner_user_id", name="uq_network_profiles_owner"),
        CheckConstraint("version >= 1", name="ck_network_profiles_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="Моя сеть")
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    auto_apply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )


class ProjectProfileApplication(Base):
    __tablename__ = "project_profile_applications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('applied', 'failed')", name="ck_profile_applications_status",
        ),
        CheckConstraint(
            "profile_version >= 1", name="ck_profile_applications_version",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True,
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("owner_network_profiles.id", ondelete="RESTRICT"), nullable=False,
    )
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
