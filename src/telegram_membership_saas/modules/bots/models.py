"""Tenant-owned Telegram bot credentials and identity (not yet a runtime dispatcher)."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class BotInstanceStatus(StrEnum):
    REGISTERED = "registered"
    DISABLED = "disabled"


class BotInstance(Base):
    __tablename__ = "bot_instances"
    __table_args__ = (
        CheckConstraint(
            "status IN ('registered', 'disabled')",
            name="ck_bot_instances_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Unique globally, not merely per tenant: one Telegram bot cannot be controlled
    # by two different projects even if RLS hides the other project.
    telegram_bot_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_ciphertext: Mapped[str] = mapped_column(String(4096), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=BotInstanceStatus.REGISTERED.value,
        server_default=BotInstanceStatus.REGISTERED.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
