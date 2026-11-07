"""Persist mutually consented bot-management ownership transfers."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, Index,
    String, Uuid, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class ProjectOwnershipTransfer(Base):
    __tablename__ = "project_ownership_transfers"
    __table_args__ = (
        CheckConstraint("from_user_id <> to_user_id", name="ck_ownership_transfer_distinct"),
        CheckConstraint(
            "status IN ('pending','accepted','cancelled','completed')",
            name="ck_ownership_transfer_status",
        ),
        CheckConstraint("expires_at > created_at", name="ck_ownership_transfer_expiry"),
        Index(
            "uq_project_one_open_transfer", "project_id", unique=True,
            postgresql_where=text("status IN ('pending', 'accepted')"),
        ),
        Index("ix_project_transfers_recipient", "to_user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False,
    )
    from_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
    )
    to_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bot_username: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    encrypted_new_token: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
