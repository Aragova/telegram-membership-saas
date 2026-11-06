"""Persistent commercial identity, independent of Telegram bots and projects.

These records are platform-protected. No tenant-facing queries or linking API
are added in this schema-only foundation.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class MerchantIdentity(Base):
    __tablename__ = "merchant_identities"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    initial_owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MerchantProjectPeriod(Base):
    __tablename__ = "merchant_project_periods"
    __table_args__ = (
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_merchant_project_period_dates",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(
        ForeignKey("merchant_identities.id", ondelete="RESTRICT"), nullable=False
    )
    # No FK: project history must survive future project archival/purge.
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MerchantChannelPeriod(Base):
    __tablename__ = "merchant_channel_periods"
    __table_args__ = (
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_merchant_channel_period_dates",
        ),
        CheckConstraint(
            "resource_type IN ('telegram_channel', 'telegram_group')",
            name="ck_merchant_channel_resource_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(
        ForeignKey("merchant_identities.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A discovered chat is not proof of legal/commercial ownership.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
