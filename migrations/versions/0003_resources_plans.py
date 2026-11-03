"""create resources and plans catalog

Revision ID: 0003_resources_plans
Revises: 0002_tenant_rls
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_resources_plans"
down_revision: str | None = "0002_tenant_rls"
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


def _disable_project_rls(table_name: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS {table_name}_tenant_policy ON {table_name}")
    op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.create_table(
        "resources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
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
            "resource_type IN ('telegram_channel', 'telegram_group')",
            name="ck_resources_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'archived')",
            name="ck_resources_status",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_resources_id_project"),
        sa.UniqueConstraint(
            "project_id",
            "resource_type",
            "external_id",
            name="uq_resources_project_type_external",
        ),
    )
    op.create_index("ix_resources_project_id", "resources", ["project_id"])

    op.create_table(
        "plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("duration_value", sa.Integer(), nullable=True),
        sa.Column("duration_unit", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
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
            "duration_unit IN ('day', 'week', 'month', 'year', 'lifetime')",
            name="ck_plans_duration_unit",
        ),
        sa.CheckConstraint(
            "(duration_unit = 'lifetime' AND duration_value IS NULL) OR "
            "(duration_unit <> 'lifetime' AND duration_value IS NOT NULL AND duration_value > 0)",
            name="ck_plans_duration_value",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_plans_status",
        ),
        sa.CheckConstraint("sort_order >= 0", name="ck_plans_sort_order"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_plans_id_project"),
    )
    op.create_index("ix_plans_project_id", "plans", ["project_id"])

    op.create_table(
        "plan_resources",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["plans.id", "plans.project_id"],
            ondelete="CASCADE",
            name="fk_plan_resources_plan_project",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            ondelete="CASCADE",
            name="fk_plan_resources_resource_project",
        ),
        sa.PrimaryKeyConstraint("plan_id", "resource_id"),
    )
    op.create_index("ix_plan_resources_project_id", "plan_resources", ["project_id"])

    _enable_project_rls("resources")
    _enable_project_rls("plans")
    _enable_project_rls("plan_resources")


def downgrade() -> None:
    _disable_project_rls("plan_resources")
    _disable_project_rls("plans")
    _disable_project_rls("resources")

    op.drop_index("ix_plan_resources_project_id", table_name="plan_resources")
    op.drop_table("plan_resources")
    op.drop_index("ix_plans_project_id", table_name="plans")
    op.drop_table("plans")
    op.drop_index("ix_resources_project_id", table_name="resources")
    op.drop_table("resources")
