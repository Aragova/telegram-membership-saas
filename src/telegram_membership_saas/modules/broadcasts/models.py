"""Tenant-scoped audience, reusable posts and durable broadcast deliveries."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class BroadcastAudience(Base):
    __tablename__ = "broadcast_audience"

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active",
    )
    first_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    last_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('active','blocked')",
                        name="ck_broadcast_audience_status"),
        CheckConstraint("telegram_user_id > 0 AND telegram_bot_id > 0",
                        name="ck_broadcast_audience_ids"),
    )


class BroadcastTemplate(Base):
    __tablename__ = "broadcast_templates"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_broadcast_template_project"),
        CheckConstraint(
            "kind IN ('text','photo','video','document','animation')",
            name="ck_broadcast_template_kind",
        ),
        CheckConstraint("status IN ('active','archived')",
                        name="ck_broadcast_template_status"),
        Index("ix_broadcast_templates_recent", "project_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("telegram_users.id"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list | None] = mapped_column(JSON)
    file_id: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(
        String(16), server_default="active", nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class BroadcastDraft(Base):
    __tablename__ = "broadcast_drafts"
    __table_args__ = (
        CheckConstraint("mode IN ('create','replace','rename')",
                        name="ck_broadcast_draft_mode"),
        ForeignKeyConstraint(
            ["template_id", "project_id"],
            ["broadcast_templates.id", "broadcast_templates.project_id"],
            name="fk_broadcast_draft_project_template",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("telegram_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    template_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )



class BroadcastCampaign(Base):
    __tablename__ = "broadcast_campaigns"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_broadcast_campaign_project"),
        UniqueConstraint(
            "project_id", "telegram_bot_id", "confirmation_key",
            name="uq_broadcast_campaign_confirmation",
        ),
        ForeignKeyConstraint(
            ["template_id", "project_id"],
            ["broadcast_templates.id", "broadcast_templates.project_id"],
            name="fk_broadcast_campaign_project_template",
        ),
        CheckConstraint("status IN ('running','paused','completed')",
                        name="ck_broadcast_campaign_status"),
        CheckConstraint(
            "kind IN ('text','photo','video','document','animation')",
            name="ck_broadcast_campaign_kind",
        ),
        Index("ix_broadcast_campaigns_project_status",
              "project_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    template_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    confirmation_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("telegram_users.id"), nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list | None] = mapped_column(JSON)
    file_id: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(
        String(16), server_default="running", nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BroadcastDelivery(Base):
    __tablename__ = "broadcast_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "project_id"],
            ["broadcast_campaigns.id", "broadcast_campaigns.project_id"],
            ondelete="CASCADE", name="fk_broadcast_delivery_project_campaign",
        ),
        UniqueConstraint("campaign_id", "telegram_user_id",
                         name="uq_broadcast_delivery_recipient"),
        CheckConstraint(
            "status IN ('pending','sending','sent','blocked','failed','unknown')",
            name="ck_broadcast_delivery_status",
        ),
        Index("ix_broadcast_delivery_due",
              "project_id", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), server_default="pending", nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(48))
