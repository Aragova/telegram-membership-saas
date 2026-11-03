"""add payment methods and manual settlement provider

Revision ID: 0005_payment_methods
Revises: 0004_pricing_core
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_payment_methods"
down_revision: str | None = "0004_pricing_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def _enable_project_rls(table_name: str) -> None:
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table_name}_tenant_policy ON {table_name}
        USING (
            {CURRENT_PROJECT} IS NOT NULL
            AND project_id = {CURRENT_PROJECT}
        )
        WITH CHECK (
            {CURRENT_PROJECT} IS NOT NULL
            AND project_id = {CURRENT_PROJECT}
        )
        """
    )


def upgrade() -> None:
    op.create_table(
        "payment_methods",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=32),
            server_default="manual_settlement",
            nullable=False,
        ),
        sa.Column("instruction_template", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('manual_settlement')",
            name="ck_payment_methods_provider",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_payment_methods_status",
        ),
        sa.CheckConstraint("sort_order >= 0", name="ck_payment_methods_sort_order"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_payment_methods_id_project"),
    )
    op.create_index("ix_payment_methods_project_id", "payment_methods", ["project_id"])

    op.create_table(
        "payment_method_currencies",
        sa.Column("payment_method_id", sa.Uuid(), nullable=False),
        sa.Column("currency_code", sa.String(length=16), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["currency_code"],
            ["currency_assets.code"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_method_id", "project_id"],
            ["payment_methods.id", "payment_methods.project_id"],
            ondelete="CASCADE",
            name="fk_payment_method_currencies_method_project",
        ),
        sa.PrimaryKeyConstraint("payment_method_id", "currency_code"),
    )
    op.create_index(
        "ix_payment_method_currencies_project_id",
        "payment_method_currencies",
        ["project_id"],
    )

    _enable_project_rls("payment_methods")
    _enable_project_rls("payment_method_currencies")


def downgrade() -> None:
    for table_name in ("payment_method_currencies", "payment_methods"):
        op.execute(f"DROP POLICY IF EXISTS {table_name}_tenant_policy ON {table_name}")
        op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "ix_payment_method_currencies_project_id",
        table_name="payment_method_currencies",
    )
    op.drop_table("payment_method_currencies")
    op.drop_index("ix_payment_methods_project_id", table_name="payment_methods")
    op.drop_table("payment_methods")
