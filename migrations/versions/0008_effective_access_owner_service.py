"""add owner service accounts and effective access projection

Revision ID: 0008_effective_access_owner_service
Revises: 0007_atomic_approval
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_effective_access_owner_service"
down_revision: str | None = "0007_atomic_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"
CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "owner_service_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("service_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "scope_type",
            sa.String(length=32),
            server_default="all_owner_projects",
            nullable=False,
        ),
        sa.Column(
            "auto_include_future_projects",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "scope_type IN ('all_owner_projects')",
            name="ck_owner_service_accounts_scope",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_owner_service_accounts_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_owner_service_accounts_revocation",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["telegram_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["service_user_id"], ["telegram_users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_user_id", name="uq_owner_service_accounts_id_owner"),
        sa.UniqueConstraint(
            "owner_user_id",
            "service_user_id",
            name="uq_owner_service_accounts_owner_service_user",
        ),
    )
    op.create_index(
        "ix_owner_service_accounts_owner_user_id",
        "owner_service_accounts",
        ["owner_user_id"],
    )
    op.create_index(
        "ix_owner_service_accounts_service_user_id",
        "owner_service_accounts",
        ["service_user_id"],
    )

    op.execute("ALTER TABLE owner_service_accounts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE owner_service_accounts FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY owner_service_accounts_owner_policy ON owner_service_accounts
        USING (owner_user_id = {CURRENT_USER})
        WITH CHECK (owner_user_id = {CURRENT_USER})
        """
    )

    op.create_table(
        "effective_access",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("desired_access", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            ondelete="CASCADE",
            name="fk_effective_access_customer_project",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            ondelete="CASCADE",
            name="fk_effective_access_resource_project",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_effective_access_id_project"),
        sa.UniqueConstraint(
            "project_id",
            "customer_id",
            "resource_id",
            name="uq_effective_access_customer_resource",
        ),
    )
    op.create_index("ix_effective_access_project_id", "effective_access", ["project_id"])
    op.create_index("ix_effective_access_customer_id", "effective_access", ["customer_id"])
    op.create_index("ix_effective_access_resource_id", "effective_access", ["resource_id"])

    op.execute("ALTER TABLE effective_access ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE effective_access FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY effective_access_tenant_policy ON effective_access
        USING ({CURRENT_PROJECT} IS NOT NULL AND project_id = {CURRENT_PROJECT})
        WITH CHECK ({CURRENT_PROJECT} IS NOT NULL AND project_id = {CURRENT_PROJECT})
        """
    )

    op.drop_constraint("ck_outbox_events_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_events_type",
        "outbox_events",
        "event_type IN ('access_recalculate', 'access_sync')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_outbox_events_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_events_type",
        "outbox_events",
        "event_type IN ('access_recalculate')",
    )

    op.execute("DROP POLICY IF EXISTS effective_access_tenant_policy ON effective_access")
    op.execute("ALTER TABLE effective_access NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE effective_access DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_effective_access_resource_id", table_name="effective_access")
    op.drop_index("ix_effective_access_customer_id", table_name="effective_access")
    op.drop_index("ix_effective_access_project_id", table_name="effective_access")
    op.drop_table("effective_access")

    op.execute(
        "DROP POLICY IF EXISTS owner_service_accounts_owner_policy ON owner_service_accounts"
    )
    op.execute("ALTER TABLE owner_service_accounts NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE owner_service_accounts DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "ix_owner_service_accounts_service_user_id",
        table_name="owner_service_accounts",
    )
    op.drop_index(
        "ix_owner_service_accounts_owner_user_id",
        table_name="owner_service_accounts",
    )
    op.drop_table("owner_service_accounts")
