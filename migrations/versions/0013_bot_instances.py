"""Register tenant-owned Telegram bot identities with encrypted credentials.

Revision ID: 0013_bot_instances
Revises: 0012_system_project_reads
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_bot_instances"
down_revision: str | None = "0012_system_project_reads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "bot_instances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("token_ciphertext", sa.String(length=4096), nullable=False),
        sa.Column(
            "status", sa.String(length=16),
            server_default="registered", nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('registered', 'disabled')",
            name="ck_bot_instances_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_bot_id", name="uq_bot_instances_telegram_bot_id"),
    )
    op.create_index("ix_bot_instances_project_id", "bot_instances", ["project_id"])
    op.execute("ALTER TABLE bot_instances ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bot_instances FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY bot_instances_tenant_policy ON bot_instances
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
    op.execute("DROP POLICY IF EXISTS bot_instances_tenant_policy ON bot_instances")
    op.execute("ALTER TABLE bot_instances NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bot_instances DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_bot_instances_project_id", table_name="bot_instances")
    op.drop_table("bot_instances")
