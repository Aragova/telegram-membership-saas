"""add Telegram join tickets and secure admission state

Revision ID: 0011_telegram_access
Revises: 0010_access_runtime
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_telegram_access"
down_revision: str | None = "0010_access_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "telegram_join_tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("access_action_id", sa.Uuid(), nullable=False),
        sa.Column("claim_attempt", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("invite_link", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending', 'approved', 'revoked', 'expired')",
            name="ck_telegram_join_tickets_status",
        ),
        sa.CheckConstraint(
            "claim_attempt > 0",
            name="ck_telegram_join_tickets_claim_attempt",
        ),
        sa.ForeignKeyConstraint(
            ["access_action_id", "project_id"],
            ["access_actions.id", "access_actions.project_id"],
            name="fk_telegram_join_tickets_action_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_telegram_join_tickets_customer_project",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            name="fk_telegram_join_tickets_resource_project",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id",
            "project_id",
            name="uq_telegram_join_tickets_id_project",
        ),
        sa.UniqueConstraint(
            "project_id",
            "access_action_id",
            name="uq_telegram_join_tickets_action",
        ),
    )
    op.create_index(
        "ix_telegram_join_tickets_project_id",
        "telegram_join_tickets",
        ["project_id"],
    )
    op.create_index(
        "ix_telegram_join_tickets_customer_id",
        "telegram_join_tickets",
        ["customer_id"],
    )
    op.create_index(
        "ix_telegram_join_tickets_resource_id",
        "telegram_join_tickets",
        ["resource_id"],
    )
    op.create_index(
        "ix_telegram_join_tickets_telegram_user_id",
        "telegram_join_tickets",
        ["telegram_user_id"],
    )
    op.create_index(
        "ix_telegram_join_tickets_lookup",
        "telegram_join_tickets",
        ["project_id", "chat_id", "telegram_user_id", "status"],
    )
    op.create_index(
        "ix_telegram_join_tickets_invite",
        "telegram_join_tickets",
        ["project_id", "invite_link"],
    )
    op.create_index(
        "uq_telegram_join_tickets_one_pending_target",
        "telegram_join_tickets",
        ["project_id", "customer_id", "resource_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.execute("ALTER TABLE telegram_join_tickets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE telegram_join_tickets FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY telegram_join_tickets_tenant_policy ON telegram_join_tickets
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
        "DROP POLICY IF EXISTS telegram_join_tickets_tenant_policy "
        "ON telegram_join_tickets"
    )
    op.execute("ALTER TABLE telegram_join_tickets NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE telegram_join_tickets DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "uq_telegram_join_tickets_one_pending_target",
        table_name="telegram_join_tickets",
    )
    op.drop_index("ix_telegram_join_tickets_invite", table_name="telegram_join_tickets")
    op.drop_index("ix_telegram_join_tickets_lookup", table_name="telegram_join_tickets")
    op.drop_index(
        "ix_telegram_join_tickets_telegram_user_id",
        table_name="telegram_join_tickets",
    )
    op.drop_index("ix_telegram_join_tickets_resource_id", table_name="telegram_join_tickets")
    op.drop_index("ix_telegram_join_tickets_customer_id", table_name="telegram_join_tickets")
    op.drop_index("ix_telegram_join_tickets_project_id", table_name="telegram_join_tickets")
    op.drop_table("telegram_join_tickets")
