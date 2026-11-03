"""add project customers and payment request snapshots

Revision ID: 0006_payment_requests
Revises: 0005_payment_methods
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_payment_requests"
down_revision: str | None = "0005_payment_methods"
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
    op.create_unique_constraint(
        "uq_plan_prices_request_identity",
        "plan_prices",
        ["id", "project_id", "plan_id", "currency_code"],
    )

    op.create_table(
        "project_customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["telegram_users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_project_customers_id_project"),
        sa.UniqueConstraint(
            "project_id",
            "user_id",
            name="uq_project_customers_project_user",
        ),
    )
    op.create_index("ix_project_customers_project_id", "project_customers", ["project_id"])
    op.create_index("ix_project_customers_user_id", "project_customers", ["user_id"])

    op.create_table(
        "payment_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("plan_price_id", sa.Uuid(), nullable=False),
        sa.Column("payment_method_id", sa.Uuid(), nullable=False),
        sa.Column("currency_code", sa.String(length=16), nullable=False),
        sa.Column("regular_amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "discount_amount",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
        sa.Column("final_amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("promotion_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="waiting_confirmation",
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
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
            "regular_amount > 0",
            name="ck_payment_requests_regular_positive",
        ),
        sa.CheckConstraint(
            "discount_amount >= 0",
            name="ck_payment_requests_discount_nonnegative",
        ),
        sa.CheckConstraint("final_amount > 0", name="ck_payment_requests_final_positive"),
        sa.CheckConstraint(
            "final_amount = regular_amount - discount_amount",
            name="ck_payment_requests_amount_math",
        ),
        sa.CheckConstraint(
            "status IN ('waiting_confirmation', 'waiting_review', 'approved', 'rejected', "
            "'cancelled', 'expired')",
            name="ck_payment_requests_status",
        ),
        sa.CheckConstraint(
            "(status = 'waiting_confirmation' AND submitted_at IS NULL "
            "AND reviewed_at IS NULL AND reviewed_by IS NULL) OR "
            "(status = 'waiting_review' AND submitted_at IS NOT NULL "
            "AND reviewed_at IS NULL AND reviewed_by IS NULL) OR "
            "(status IN ('approved', 'rejected') AND submitted_at IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL) OR "
            "(status IN ('cancelled', 'expired') AND submitted_at IS NULL "
            "AND reviewed_at IS NULL AND reviewed_by IS NULL)",
            name="ck_payment_requests_state_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_payment_requests_customer_project",
        ),
        sa.ForeignKeyConstraint(
            ["plan_price_id", "project_id", "plan_id", "currency_code"],
            [
                "plan_prices.id",
                "plan_prices.project_id",
                "plan_prices.plan_id",
                "plan_prices.currency_code",
            ],
            name="fk_payment_requests_price_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["payment_method_id", "project_id"],
            ["payment_methods.id", "payment_methods.project_id"],
            name="fk_payment_requests_method_project",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["telegram_users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_payment_requests_id_project"),
    )
    op.create_index("ix_payment_requests_project_id", "payment_requests", ["project_id"])
    op.create_index("ix_payment_requests_customer_id", "payment_requests", ["customer_id"])
    op.create_index("ix_payment_requests_status", "payment_requests", ["status"])

    _enable_project_rls("project_customers")
    _enable_project_rls("payment_requests")


def downgrade() -> None:
    for table_name in ("payment_requests", "project_customers"):
        op.execute(f"DROP POLICY IF EXISTS {table_name}_tenant_policy ON {table_name}")
        op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_payment_requests_status", table_name="payment_requests")
    op.drop_index("ix_payment_requests_customer_id", table_name="payment_requests")
    op.drop_index("ix_payment_requests_project_id", table_name="payment_requests")
    op.drop_table("payment_requests")
    op.drop_index("ix_project_customers_user_id", table_name="project_customers")
    op.drop_index("ix_project_customers_project_id", table_name="project_customers")
    op.drop_table("project_customers")
    op.drop_constraint(
        "uq_plan_prices_request_identity",
        "plan_prices",
        type_="unique",
    )
