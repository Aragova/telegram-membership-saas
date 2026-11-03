from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class OwnerServiceScope(StrEnum):
    ALL_OWNER_PROJECTS = "all_owner_projects"


class OwnerServiceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class OwnerServiceAccount(Base):
    __tablename__ = "owner_service_accounts"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_owner_service_accounts_id_owner"),
        CheckConstraint(
            "scope_type IN ('all_owner_projects')",
            name="ck_owner_service_accounts_scope",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_owner_service_accounts_status",
        ),
        CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_owner_service_accounts_revocation",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    service_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scope_type: Mapped[str] = mapped_column(
        String(32),
        default=OwnerServiceScope.ALL_OWNER_PROJECTS.value,
        server_default=OwnerServiceScope.ALL_OWNER_PROJECTS.value,
        nullable=False,
    )
    auto_include_future_projects: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=OwnerServiceStatus.ACTIVE.value,
        server_default=OwnerServiceStatus.ACTIVE.value,
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
