"""Record owner obligation decisions while keeping payment history immutable.

Revision ID: 0017_owner_obligation_audit
Revises: 0016_bot_deletion_lifecycle
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_owner_obligation_audit"
down_revision: str | None = "0016_bot_deletion_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"
ACTOR = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "owner_obligation_audit",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "project_id", sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "actor_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index(
        "ix_owner_obligation_audit_project_id", "owner_obligation_audit", ["project_id"],
    )
    op.execute("ALTER TABLE owner_obligation_audit ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE owner_obligation_audit FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY owner_obligation_audit_select ON owner_obligation_audit
        FOR SELECT USING (
            project_id = {PROJECT}
            AND EXISTS (
                SELECT 1 FROM projects p
                WHERE p.id = project_id AND p.owner_user_id = {ACTOR}
            )
        )
    """)
    op.execute(f"""
        CREATE POLICY owner_obligation_audit_insert ON owner_obligation_audit
        FOR INSERT WITH CHECK (
            project_id = {PROJECT} AND actor_id = {ACTOR}
            AND EXISTS (
                SELECT 1 FROM projects p
                WHERE p.id = project_id AND p.owner_user_id = {ACTOR}
            )
        )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS owner_obligation_audit_insert ON owner_obligation_audit")
    op.execute("DROP POLICY IF EXISTS owner_obligation_audit_select ON owner_obligation_audit")
    op.execute("ALTER TABLE owner_obligation_audit NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE owner_obligation_audit DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_owner_obligation_audit_project_id", table_name="owner_obligation_audit")
    op.drop_table("owner_obligation_audit")
