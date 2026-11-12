"""Persist Telegram Stars precheckout authorization before Crypto Pay issuance.

Revision ID: 0063_file_stars_authorized
Revises: 0062_crypto_recovery_delivery
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_file_stars_authorized"
down_revision: str | None = "0062_crypto_recovery_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A positive Telegram precheckout acknowledgement can already authorize
    # a real Stars charge. The successful_payment update may arrive later.
    # The pending authorization must survive a bot restart and guard against
    # creating a second charge through Crypto Pay.
    op.add_column(
        "file_sale_purchases",
        sa.Column("stars_authorized_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    raise RuntimeError("Cannot discard durable Stars authorization safety evidence.")
