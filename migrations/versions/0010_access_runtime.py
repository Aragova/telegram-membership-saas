"""add access action runtime queue

Revision ID: 0010_access_runtime
Revises: 0009_access_state_core
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_access_runtime"
down_revision: str | None = "0009_access_state_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "access_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("desired_revision", sa.Integer(), nullable=False),
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
            "desired_revision >= 0",
            name="ck_access_actions_desired_revision",
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
            ["project_id", "customer_id", "resource_id"],
            [
                "access_states.project_id",
                "access_states.customer_id",
                "access_states.resource_id",
            ],
            name="fk_access_actions_state_target",
            ondelete="CASCADE",
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

    op.execute("ALTER TABLE access_actions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE access_actions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY access_actions_tenant_policy ON access_actions
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
    op.execute("DROP POLICY IF EXISTS access_actions_tenant_policy ON access_actions")
    op.execute("ALTER TABLE access_actions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE access_actions DISABLE ROW LEVEL SECURITY")
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
