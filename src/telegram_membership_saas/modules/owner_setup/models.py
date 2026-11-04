"""Durable owner wizard state. Never store payment receipts or BotFather tokens here."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class OwnerSetupSession(Base):
    __tablename__ = "owner_setup_sessions"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("telegram_users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    selected_project_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL")
    )
    step: Mapped[str | None] = mapped_column(String(48), nullable=True)
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC) + timedelta(minutes=15),
        nullable=False,
    )
