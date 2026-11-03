"""add effective access projection

Revision ID: 0008_effective_access_projection
Revises: 0007_atomic_approval
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_effective_access_projection"
down_revision: str | None = "0007_atomic_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "effective_access",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column(
            "has_access",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "active_grant_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "revision",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("next_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
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
            "active_grant_count >= 0",
            name="ck_effective_access_grant_count",
        ),
        sa.CheckConstraint(
            "(has_access AND active_grant_count > 0) OR "
            "(NOT has_access AND active_grant_count = 0)",
            name="ck_effective_access_consistency",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_effective_access_revision"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_effective_access_customer_project",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            name="fk_effective_access_resource_project",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "project_id",
            name="uq_effective_access_id_project",
        ),
        sa.UniqueConstraint(
            "project_id",
            "customer_id",
            "resource_id",
            name="uq_effective_access_target",
        ),
    )
    op.create_index("ix_effective_access_project_id", "effective_access", ["project_id"])
    op.create_index("ix_effective_access_customer_id", "effective_access", ["customer_id"])
    op.create_index("ix_effective_access_resource_id", "effective_access", ["resource_id"])
    op.create_index(
        "ix_effective_access_next_change",
        "effective_access",
        ["project_id", "next_change_at"],
    )

    op.execute("ALTER TABLE effective_access ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE effective_access FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY effective_access_tenant_policy ON effective_access
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


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS effective_access_tenant_policy ON effective_access"
    )
    op.execute("ALTER TABLE effective_access NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE effective_access DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_effective_access_next_change", table_name="effective_access")
    op.drop_index("ix_effective_access_resource_id", table_name="effective_access")
    op.drop_index("ix_effective_access_customer_id", table_name="effective_access")
    op.drop_index("ix_effective_access_project_id", table_name="effective_access")
    op.drop_table("effective_access")
