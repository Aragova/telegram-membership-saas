"""Persist Crypto Pay paid membership sources and sold resource snapshots.

Revision ID: 0061_crypto_paid_sources
Revises: 0060_crypto_issuing_guard
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061_crypto_paid_sources"
down_revision: str | None = "0060_crypto_issuing_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "crypto_pay_invoices",
        sa.Column(
            "resource_ids_snapshot", sa.JSON(), nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        "crypto_pay_invoices",
        sa.Column("last_delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("last_crypto_invoice_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscriptions_last_crypto_invoice_project",
        "subscriptions", "crypto_pay_invoices",
        ["last_crypto_invoice_id", "project_id"], ["id", "project_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "ck_subscriptions_last_paid_source", "subscriptions", type_="check",
    )
    op.create_check_constraint(
        "ck_subscriptions_last_paid_source", "subscriptions",
        "last_payment_id IS NOT NULL OR last_star_order_id IS NOT NULL "
        "OR last_crypto_invoice_id IS NOT NULL",
    )
    op.drop_constraint(
        "ck_entitlement_grants_source", "entitlement_grants", type_="check",
    )
    op.create_check_constraint(
        "ck_entitlement_grants_source", "entitlement_grants",
        "source IN ('payment','stars_payment','crypto_payment','manual',"
        "'owner_service','promocode','trial','referral','import','compensation')",
    )


def downgrade() -> None:
    raise RuntimeError("Cannot downgrade paid Crypto Pay subscription and resource evidence.")
