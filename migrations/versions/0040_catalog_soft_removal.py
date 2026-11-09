"""Soft-remove unpaid checkout catalog items without losing immutable invoices.

Revision ID: 0040_catalog_soft_removal
Revises: 0039_file_payment_visibility
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_catalog_soft_removal"
down_revision: str | None = "0039_file_payment_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("resources", "plans", "file_sale_folders", "file_sale_products")


def upgrade() -> None:
    # Historical orders/payments remain in their existing immutable tables.
    # removed_at controls owner/buyer catalog visibility only.
    for table in _TABLES:
        op.add_column(table, sa.Column(
            "removed_at", sa.DateTime(timezone=True), nullable=True,
        ))


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "removed_at")
