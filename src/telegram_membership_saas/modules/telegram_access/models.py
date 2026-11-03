from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class TelegramJoinTicketStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REVOKED = "revoked"
    EXPIRED = "expired"


class TelegramJoinTicket(Base):
    __tablename__ = "telegram_join_tickets"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_telegram_join_tickets_id_project"),
        UniqueConstraint(
            "project_id",
            "access_action_id",
            name="uq_telegram_join_tickets_action",
        ),
        ForeignKeyConstraint(
            ["access_action_id", "project_id"],
            ["access_actions.id", "access_actions.project_id"],
            name="fk_telegram_join_tickets_action_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_telegram_join_tickets_customer_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            name="fk_telegram_join_tickets_resource_project",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'revoked', 'expired')",
            name="ck_telegram_join_tickets_status",
        ),
        CheckConstraint(
            "claim_attempt > 0",
            name="ck_telegram_join_tickets_claim_attempt",
        ),
        Index(
            "uq_telegram_join_tickets_one_pending_target",
            "project_id",
            "customer_id",
            "resource_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_telegram_join_tickets_lookup",
            "project_id",
            "chat_id",
            "telegram_user_id",
            "status",
        ),
        Index(
            "ix_telegram_join_tickets_invite",
            "project_id",
            "invite_link",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    access_action_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    claim_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    invite_link: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=TelegramJoinTicketStatus.PENDING.value,
        server_default=TelegramJoinTicketStatus.PENDING.value,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
