"""Project-scoped fiat file prices and independently reviewed manual purchases."""
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class FileManualPrice(Base):
    __tablename__ = "file_sale_manual_prices"
    __table_args__ = (
        ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_manual_price_product",
            ondelete="CASCADE",
        ),
        CheckConstraint("amount > 0", name="ck_file_manual_price_positive"),
    )

    product_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    currency_code: Mapped[str] = mapped_column(
        String(16), ForeignKey("currency_assets.code", ondelete="RESTRICT"),
        primary_key=True,
    )
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)


class FileManualOrder(Base):
    __tablename__ = "file_sale_manual_orders"
    __table_args__ = (
        ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_manual_order_product", ondelete="RESTRICT",
        ),
        CheckConstraint("amount > 0", name="ck_file_manual_order_positive"),
        CheckConstraint(
            "status IN ('pending','submitted','approved','rejected','cancelled')",
            name="ck_file_manual_order_status",
        ),
        Index("ix_file_manual_orders_buyer", "project_id", "telegram_user_id", "status"),
        Index("ix_file_manual_orders_reviews", "project_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(16), nullable=False)
    payment_method_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payment_method_name: Mapped[str] = mapped_column(String(120), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    receipt_kind: Mapped[str | None] = mapped_column(String(16))
    receipt_file_id: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
