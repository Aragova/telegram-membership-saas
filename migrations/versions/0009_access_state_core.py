"""add access desired-vs-actual state

Revision ID: 0009_access_state_core
Revises: 0008_effective_access_projection
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_access_state_core"
down_revision: str | None = "0008_effective_access_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "access_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column(
            "desired_access",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "desired_revision",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "actual_state",
            sa.String(length=16),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
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
            "desired_revision >= 0",
            name="ck_access_states_desired_revision",
        ),
        sa.CheckConstraint(
            "actual_state IN ('unknown', 'granted', 'revoked')",
            name="ck_access_states_actual_state",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "customer_id", "resource_id"],
            [
                "effective_access.project_id",
                "effective_access.customer_id",
                "effective_access.resource_id",
            ],
            name="fk_access_states_effective_access_target",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "project_id",
            name="uq_access_states_id_project",
        ),
        sa.UniqueConstraint(
            "project_id",
            "customer_id",
            "resource_id",
            name="uq_access_states_target",
        ),
    )
    op.create_index("ix_access_states_project_id", "access_states", ["project_id"])
    op.create_index("ix_access_states_customer_id", "access_states", ["customer_id"])
    op.create_index("ix_access_states_resource_id", "access_states", ["resource_id"])

    op.execute("ALTER TABLE access_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE access_states FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY access_states_tenant_policy ON access_states
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
    op.execute("DROP POLICY IF EXISTS access_states_tenant_policy ON access_states")
    op.execute("ALTER TABLE access_states NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE access_states DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_access_states_resource_id", table_name="access_states")
    op.drop_index("ix_access_states_customer_id", table_name="access_states")
    op.drop_index("ix_access_states_project_id", table_name="access_states")
    op.drop_table("access_states")
