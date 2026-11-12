"""Tenant-owned Crypto Pay credentials and frozen provider invoice evidence.

There is deliberately NO path that marks an invoice paid in this foundation
slice: future fulfillment must atomically add the file delivery or membership
grant *before* changing the order state. No fake paid status or dead button.
"""
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
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class CryptoPayApp(Base):
    __tablename__ = "crypto_pay_apps"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_crypto_pay_app_project"),
        CheckConstraint(
            "environment IN ('testnet','mainnet')",
            name="ck_crypto_pay_app_environment",
        ),
        CheckConstraint(
            "status IN ('active','retired')",
            name="ck_crypto_pay_app_status",
        ),
        Index(
            "uq_crypto_pay_active_bot", "project_id", "telegram_bot_id",
            unique=True, postgresql_where=text("status='active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False,
    )
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
    )
    token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    app_label: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp(),
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CryptoPayInvoice(Base):
    """Frozen quote. This table alone MUST NOT grant subscription/file access."""

    __tablename__ = "crypto_pay_invoices"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_crypto_pay_invoice_project"),
        UniqueConstraint("payload", name="uq_crypto_pay_invoice_payload"),
        UniqueConstraint(
            "app_id", "provider_invoice_id", name="uq_crypto_pay_app_invoice",
        ),
        ForeignKeyConstraint(
            ["app_id", "project_id"], ["crypto_pay_apps.id", "crypto_pay_apps.project_id"],
            ondelete="RESTRICT", name="fk_crypto_pay_invoice_app_project",
        ),
        CheckConstraint(
            "(kind='subscription' AND plan_id IS NOT NULL "
            "AND plan_price_id IS NOT NULL AND product_id IS NULL "
            "AND customer_id IS NOT NULL AND duration_unit IS NOT NULL)"
            " OR (kind='file' AND product_id IS NOT NULL AND plan_id IS NULL "
            "AND plan_price_id IS NULL AND customer_id IS NULL "
            "AND duration_unit IS NULL)",
            name="ck_crypto_pay_invoice_target",
        ),
        CheckConstraint(
            "amount > 0", name="ck_crypto_pay_invoice_amount",
        ),
        CheckConstraint(
            "currency_type IN ('fiat','crypto')",
            name="ck_crypto_pay_invoice_currency_type",
        ),
        CheckConstraint(
            "status IN ('reserved','issuing','issued','paid','expired','cancelled')",
            name="ck_crypto_pay_invoice_status",
        ),
        CheckConstraint(
            "(status IN ('reserved','issuing') AND provider_invoice_id IS NULL "
            "AND paid_at IS NULL) "
            "OR (status='issued' AND provider_invoice_id IS NOT NULL "
            "AND paid_at IS NULL) "
            "OR (status='paid' AND provider_invoice_id IS NOT NULL "
            "AND paid_at IS NOT NULL) "
            "OR (status IN ('expired','cancelled') AND paid_at IS NULL)",
            name="ck_crypto_pay_invoice_state",
        ),
        Index("ix_crypto_pay_invoice_pending", "app_id", "status", "created_at"),
        Index(
            "ix_crypto_pay_invoice_buyer",
            "project_id", "telegram_bot_id", "telegram_user_id", "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False,
    )
    app_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    telegram_bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    customer_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    plan_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    plan_price_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    product_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    name_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    duration_value: Mapped[int | None] = mapped_column()
    duration_unit: Mapped[str | None] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(16), nullable=False)
    currency_type: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_invoice_id: Mapped[int | None] = mapped_column(BigInteger)
    invoice_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="reserved", server_default="reserved",
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp(),
    )
