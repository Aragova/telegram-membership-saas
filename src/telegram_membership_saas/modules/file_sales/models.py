"""Tenant-scoped digital file catalog, immutable Stars charges and redelivery."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
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
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    stars_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft",
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        UniqueConstraint("id", "project_id", name="uq_file_purchase_id_project"),
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


class FileStarAuthorization(Base):
    """Immutable, bot/project-scoped record of Telegram precheckout approval.

    Stored separately: the paid file purchase row remains protected by its
    immutable financial trigger and is only updated by actual Stars evidence.
    """
    __tablename__ = "file_sale_star_authorizations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["purchase_id", "project_id"],
            ["file_sale_purchases.id", "file_sale_purchases.project_id"],
            ondelete="RESTRICT", name="fk_file_star_auth_purchase_project",
        ),
        UniqueConstraint(
            "purchase_id", "project_id", name="uq_file_star_auth_project",
        ),
    )

    purchase_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True,
    )
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class FileStarAuthorizationRejection(Base):
    """Append-only proof that a specific Stars precheckout was rejected by Telegram.

    Only an explicitly definitive Telegram API rejection authorizes insertion.
    Old invoices with rejection evidence may not be re-authorized; buyers
    must obtain a new invoice if they still want to use Stars.
    """
    __tablename__ = "file_sale_star_authorization_rejections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["purchase_id", "project_id"],
            [
                "file_sale_star_authorizations.purchase_id",
                "file_sale_star_authorizations.project_id",
            ],
            ondelete="RESTRICT",
            name="fk_file_star_auth_rejection_project",
        ),
    )

    purchase_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class FileStarRefund(Base):
    """Immutable, authenticated Telegram Stars refund event for a paid file."""

    __tablename__ = "file_sale_star_refunds"
    __table_args__ = (
        UniqueConstraint(
            "purchase_id", name="uq_file_star_refund_purchase",
        ),
        UniqueConstraint(
            "telegram_bot_id", "telegram_charge_id",
            name="uq_file_star_refund_bot_charge",
        ),
        CheckConstraint(
            "stars_amount BETWEEN 1 AND 250000",
            name="ck_file_star_refund_amount",
        ),
        CheckConstraint(
            "length(telegram_charge_id)>0",
            name="ck_file_star_refund_charge_id",
        ),
        Index("ix_file_star_refund_paid_at", "refunded_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False,
    )
    purchase_id: Mapped[UUID] = mapped_column(
        ForeignKey("file_sale_purchases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_charge_id: Mapped[str] = mapped_column(String(256), nullable=False)
    invoice_payload: Mapped[str] = mapped_column(String(128), nullable=False)
    stars_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    refunded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
