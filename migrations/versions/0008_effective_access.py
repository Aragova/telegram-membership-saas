"""add effective access projection and external action queue

Revision ID: 0008_effective_access
Revises: 0007_atomic_approval
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_effective_access"
down_revision: str | None = "0007_atomic_approval"
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
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_effective_access_revision",
        ),
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
            "actual_state IN ('unknown', 'granted', 'revoked')",
            name="ck_access_states_actual_state",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_access_states_customer_project",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            name="fk_access_states_resource_project",
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

    op.create_table(
        "access_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("projection_revision", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=16), nullable=False),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
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
            "action_type IN ('grant', 'revoke')",
            name="ck_access_actions_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', "
            "'failed_retryable', 'failed_permanent', 'superseded')",
            name="ck_access_actions_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_access_actions_attempt_count",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_access_actions_customer_project",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            name="fk_access_actions_resource_project",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "project_id",
            name="uq_access_actions_id_project",
        ),
        sa.UniqueConstraint(
            "project_id",
            "dedupe_key",
            name="uq_access_actions_project_dedupe",
        ),
    )
    op.create_index("ix_access_actions_project_id", "access_actions", ["project_id"])
    op.create_index("ix_access_actions_customer_id", "access_actions", ["customer_id"])
    op.create_index("ix_access_actions_resource_id", "access_actions", ["resource_id"])
    op.create_index(
        "ix_access_actions_status_available",
        "access_actions",
        ["project_id", "status", "available_at"],
    )
    op.create_index(
        "uq_access_actions_one_processing_target",
        "access_actions",
        ["project_id", "customer_id", "resource_id"],
        unique=True,
        postgresql_where=sa.text("status = 'processing'"),
    )

    for table_name in ("effective_access", "access_states", "access_actions"):
        _enable_project_rls(table_name)


def downgrade() -> None:
    for table_name in ("access_actions", "access_states", "effective_access"):
        op.execute(f"DROP POLICY IF EXISTS {table_name}_tenant_policy ON {table_name}")
        op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")

    op.drop_index(
        "uq_access_actions_one_processing_target",
        table_name="access_actions",
    )
    op.drop_index(
        "ix_access_actions_status_available",
        table_name="access_actions",
    )
    op.drop_index("ix_access_actions_resource_id", table_name="access_actions")
    op.drop_index("ix_access_actions_customer_id", table_name="access_actions")
    op.drop_index("ix_access_actions_project_id", table_name="access_actions")
    op.drop_table("access_actions")

    op.drop_index("ix_access_states_resource_id", table_name="access_states")
    op.drop_index("ix_access_states_customer_id", table_name="access_states")
    op.drop_index("ix_access_states_project_id", table_name="access_states")
    op.drop_table("access_states")

    op.drop_index("ix_effective_access_next_change", table_name="effective_access")
    op.drop_index("ix_effective_access_resource_id", table_name="effective_access")
    op.drop_index("ix_effective_access_customer_id", table_name="effective_access")
    op.drop_index("ix_effective_access_project_id", table_name="effective_access")
    op.drop_table("effective_access")
