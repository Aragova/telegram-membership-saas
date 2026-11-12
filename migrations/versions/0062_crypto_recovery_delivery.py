"""Bound Crypto Pay reconciliation and durable file-delivery retry windows.

Revision ID: 0062_crypto_recovery_delivery
Revises: 0061_crypto_paid_sources
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0062_crypto_recovery_delivery"
down_revision: str | None = "0061_crypto_paid_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crypto_pay_invoices",
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crypto_pay_invoices",
        sa.Column("next_delivery_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "crypto_pay_invoices",
        sa.Column(
            "delivery_attempt_count", sa.Integer(), nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_crypto_pay_delivery_attempts",
        "crypto_pay_invoices", "delivery_attempt_count >= 0",
    )
    op.create_index(
        "ix_crypto_pay_recovery_due", "crypto_pay_invoices",
        ["project_id", "telegram_bot_id", "status", "next_check_at"],
    )


def downgrade() -> None:
    raise RuntimeError("Cannot downgrade durable Crypto Pay recovery/delivery evidence.")
