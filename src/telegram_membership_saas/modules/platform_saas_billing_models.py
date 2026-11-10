"""Privileged, dormant ViMi Pay SaaS invoice and receipt records.

Do not import these into tenant commerce handlers or expose direct table CRUD.
The database migration enforces the source checks, RLS and immutability.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class PlatformSaasOwnerExemption(Base):
    __tablename__ = "platform_saas_owner_exemptions"
    __table_args__ = (
        CheckConstraint(
            "proof_kind = 'telegram_creator_verified'",
            name="ck_saas_exemption_proof",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at > verified_at",
            name="ck_saas_exemption_end",
        ),
        Index(
            "uq_saas_owner_exempt_active_bot",
            "bot_instance_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_saas_owner_exempt_project", "project_id", "merchant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(
        ForeignKey("merchant_identities.id", ondelete="RESTRICT"), nullable=False,
    )
    project_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    bot_instance_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    channel_chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
    )
    verified_by: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
    )
    proof_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp(),
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformSaasInvoice(Base):
    __tablename__ = "platform_saas_invoices"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'prepayment' AND bot_instance_id IS NOT NULL) OR "
            "(kind = 'commission' AND bot_instance_id IS NULL)",
            name="ck_saas_invoice_kind_bot",
        ),
        CheckConstraint(
            "status IN ('draft','issued','void')", name="ck_saas_invoice_status",
        ),
        CheckConstraint("total_rub > 0", name="ck_saas_invoice_total"),
        CheckConstraint(
            "period_end > period_start AND due_at >= period_start",
            name="ck_saas_invoice_period",
        ),
        Index("ix_saas_invoice_owner", "owner_user_id", "due_at"),
        Index("ix_saas_invoice_merchant", "merchant_id", "period_start"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    invoice_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    merchant_id: Mapped[UUID] = mapped_column(
        ForeignKey("merchant_identities.id", ondelete="RESTRICT"), nullable=False,
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    bot_instance_id: Mapped[UUID | None] = mapped_column(Uuid())
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_rub: Mapped[Decimal] = mapped_column(Numeric(38, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), server_default="draft", nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False,
    )
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformSaasInvoiceLine(Base):
    __tablename__ = "platform_saas_invoice_lines"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('bot_fee','subscription','file_manual')",
            name="ck_saas_line_kind",
        ),
        CheckConstraint(
            "original_amount > 0 AND rub_per_unit > 0 "
            "AND fee_rate > 0 AND fee_rate <= 1 AND charge_rub > 0",
            name="ck_saas_line_amounts",
        ),
        Index(
            "uq_saas_once_per_paid_source", "source_kind", "source_id",
            unique=True,
            postgresql_where=text(
                "source_kind IN ('subscription','file_manual')",
            ),
        ),
        Index(
            "uq_saas_invoice_line_local_source",
            "invoice_id", "source_kind", "source_id", unique=True,
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    invoice_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_saas_invoices.id", ondelete="RESTRICT"), nullable=False,
    )
    project_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    original_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    original_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    rub_per_unit: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    fee_rate: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    charge_rub: Mapped[Decimal] = mapped_column(Numeric(38, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False,
    )


class PlatformSaasReceipt(Base):
    __tablename__ = "platform_saas_receipts"
    __table_args__ = (
        CheckConstraint("amount_rub > 0", name="ck_saas_receipt_positive"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    invoice_id: Mapped[UUID] = mapped_column(
        ForeignKey("platform_saas_invoices.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    confirmed_by: Mapped[UUID] = mapped_column(
        ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
    )
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(38, 2), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False,
    )
    reference: Mapped[str | None] = mapped_column(String(160))
