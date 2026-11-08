"""File-sale-only payment availability, independent of subscription methods."""
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, ForeignKey, Uuid, true
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class FileSaleMethodVisibility(Base):
    __tablename__ = "file_sale_method_visibility"

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    telegram_bot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("bot_instances.telegram_bot_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # UUID(int=0) represents native automatic Stars. Every other key is
    # the id of a configured manual PaymentMethod for this same project.
    method_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )
