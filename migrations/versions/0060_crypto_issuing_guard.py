"""Guard uncertain Crypto Pay createInvoice outcome against duplicate provider charges.

Revision ID: 0060_crypto_issuing_guard
Revises: 0059_crypto_pay_foundation
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060_crypto_issuing_guard"
down_revision: str | None = "0059_crypto_pay_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A timeout after provider acceptance is ambiguous. A committed 'issuing'
    # reservation MUST be reconciled, never automatically issued a second time.
    op.drop_constraint(
        "ck_crypto_pay_invoice_status", "crypto_pay_invoices", type_="check",
    )
    op.drop_constraint(
        "ck_crypto_pay_invoice_state", "crypto_pay_invoices", type_="check",
    )
    op.create_check_constraint(
        "ck_crypto_pay_invoice_status", "crypto_pay_invoices",
        "status IN ('reserved','issuing','issued','paid','expired','cancelled')",
    )
    op.create_check_constraint(
        "ck_crypto_pay_invoice_state", "crypto_pay_invoices",
        "(status IN ('reserved','issuing') AND provider_invoice_id IS NULL "
        "AND paid_at IS NULL) OR "
        "(status='issued' AND provider_invoice_id IS NOT NULL AND paid_at IS NULL) OR "
        "(status='paid' AND provider_invoice_id IS NOT NULL AND paid_at IS NOT NULL) OR "
        "(status IN ('expired','cancelled') AND paid_at IS NULL)",
    )


def downgrade() -> None:
    raise RuntimeError("Refuse to downgrade: uncertain Crypto Pay invoices must be reconciled.")
