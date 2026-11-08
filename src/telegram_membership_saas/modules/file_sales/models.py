"""Tenant-scoped digital file catalog, immutable Stars charges and redelivery."""
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
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class FileFolder(Base):
    __tablename__ = "file_sale_folders"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_file_folder_project"),
        CheckConstraint("status IN ('active', 'archived')",
                        name="ck_file_folder_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class FileProduct(Base):
    __tablename__ = "file_sale_products"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_file_product_project"),
        ForeignKeyConstraint(
            ["folder_id", "project_id"],
            ["file_sale_folders.id", "file_sale_folders.project_id"],
            name="fk_file_product_folder_project",
        ),
        CheckConstraint("status IN ('draft','published','archived')",
                        name="ck_file_product_status"),
        CheckConstraint("stars_price BETWEEN 1 AND 250000",
                        name="ck_file_product_price"),
        Index("ix_file_sale_catalog", "project_id", "status", "folder_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    folder_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stars_price: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class FileAsset(Base):
    __tablename__ = "file_sale_assets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_asset_product_project",
        ),
        UniqueConstraint("product_id", "sort_order", name="uq_file_asset_order"),
        CheckConstraint("kind IN ('document','photo','video','audio','animation')",
                        name="ck_file_asset_kind"),
        CheckConstraint("sort_order BETWEEN 0 AND 19",
                        name="ck_file_asset_position"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    product_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    file_id: Mapped[str] = mapped_column(String(512), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class FilePrice(Base):
    """Explicit fiat price per product; never infer from XTR."""
    __tablename__ = "file_sale_prices"
    __table_args__ = (
        ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_price_product_project",
        ),
        CheckConstraint("amount > 0 AND amount <= 1000000000",
                        name="ck_file_price_amount"),
    )

    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    product_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    currency_code: Mapped[str] = mapped_column(
        String(16), ForeignKey("currency_assets.code", ondelete="RESTRICT"),
        primary_key=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)


class FileManualPurchase(Base):
    """Isolated file-payment ledger: no Subscription/Entitlement grant."""
    __tablename__ = "file_sale_manual_purchases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_manual_product_project", ondelete="CASCADE",
        ),
        CheckConstraint("amount > 0 AND amount <= 1000000000",
                        name="ck_file_manual_amount"),
        CheckConstraint(
            "status IN ('waiting_confirmation','waiting_review',"
            "'paid','rejected','cancelled')",
            name="ck_file_manual_status",
        ),
        Index("ix_file_manual_buyer", "project_id", "telegram_bot_id",
              "telegram_user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(16), nullable=False)
    payment_method_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payment_method_name: Mapped[str] = mapped_column(String(120), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="waiting_confirmation",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt_forwarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FilePurchase(Base):
    __tablename__ = "file_sale_purchases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["product_id", "project_id"],
            ["file_sale_products.id", "file_sale_products.project_id"],
            name="fk_file_purchase_product_project",
            ondelete="CASCADE",
        ),
        CheckConstraint("status IN ('pending','paid')",
                        name="ck_file_purchase_status"),
        CheckConstraint("stars_amount BETWEEN 1 AND 250000",
                        name="ck_file_purchase_stars"),
        UniqueConstraint("invoice_payload", name="uq_file_purchase_payload"),
        UniqueConstraint("telegram_charge_id", name="uq_file_purchase_charge"),
        Index("ix_file_purchase_buyer", "project_id", "telegram_user_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    product_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stars_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    invoice_payload: Mapped[str] = mapped_column(String(128), nullable=False)
    telegram_charge_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
