"""RLS-protected liveness records: status is never derived from registration alone."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class CustomerBotRuntimeHealth(Base):
    __tablename__ = "customer_bot_runtime_health"
    __table_args__ = (
        CheckConstraint(
            "state IN ('setup_needed', 'queued', 'starting', 'running', 'stopped', 'error')",
            name="ck_customer_bot_runtime_health_state",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    instance_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
