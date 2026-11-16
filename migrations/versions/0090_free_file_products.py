"""Allow an explicit zero-price catalog product without creating a payment.

Revision ID: 0090_free_file_products
Revises: 0089_legacy_proof_grace

No financial rows or historical sale prices are modified. Existing products
remain paid by default; free download is an explicit owner setting.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0090_free_file_products"
down_revision: str | None = "0089_legacy_proof_grace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "file_sale_products",
        sa.Column(
            "is_free", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("file_sale_products", "is_free")
