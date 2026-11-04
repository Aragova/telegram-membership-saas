"""Durable, per-user owner onboarding steps with strict RLS.

Revision ID: 0014_owner_setup_sessions
Revises: 0013_bot_instances
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_owner_setup_sessions"
down_revision: str | None = "0013_bot_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "owner_setup_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("selected_project_id", sa.Uuid(), nullable=True),
        sa.Column("step", sa.String(length=48), nullable=True),
        sa.Column("prompt_message_id", sa.BigInteger(), nullable=True),
        sa.Column("generation", sa.String(length=24), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["telegram_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["selected_project_id"], ["projects.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "ix_owner_setup_sessions_selected_project_id", "owner_setup_sessions",
        ["selected_project_id"]
    )
    op.execute("ALTER TABLE owner_setup_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE owner_setup_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY owner_setup_sessions_actor ON owner_setup_sessions
        USING (user_id = {CURRENT_USER})
        WITH CHECK (user_id = {CURRENT_USER})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS owner_setup_sessions_actor ON owner_setup_sessions")
    op.execute("DROP INDEX IF EXISTS ix_owner_setup_sessions_selected_project_id")
    op.drop_table("owner_setup_sessions")
