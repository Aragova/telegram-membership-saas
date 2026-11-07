"""Permit only project support ban/unban audit inserts, preserving owner policy.

Revision ID: 0035_support_moderation_audit
Revises: 0034_buyer_management
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0035_support_moderation_audit"
down_revision: str | None = "0034_buyer_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"
ACTOR = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"

OWNER_POLICY = f"""
    project_id = {PROJECT} AND actor_id = {ACTOR}
    AND EXISTS (
        SELECT 1 FROM projects p
        WHERE p.id = project_id AND p.owner_user_id = {ACTOR}
    )
"""

SUPPORT_POLICY = f"""
    {OWNER_POLICY}
    OR (
        project_id = {PROJECT} AND actor_id = {ACTOR}
        AND action IN ('customer_ban', 'customer_unban')
        AND EXISTS (
            SELECT 1 FROM project_members m
            WHERE m.project_id = project_id
              AND m.user_id = {ACTOR}
              AND m.role = 'support'
        )
        AND EXISTS (
            SELECT 1 FROM project_customers c
            WHERE c.id = target_id AND c.project_id = project_id
        )
    )
"""


def upgrade() -> None:
    # Keep the audit SELECT owner-only. Updates and deletes remain forbidden.
    op.execute(
        "DROP POLICY IF EXISTS owner_obligation_audit_insert "
        "ON owner_obligation_audit"
    )
    op.execute(f"""
        CREATE POLICY owner_obligation_audit_insert ON owner_obligation_audit
        FOR INSERT WITH CHECK ({SUPPORT_POLICY})
    """)


def downgrade() -> None:
    # Rollback restores owner-only inserts; historic staff audits stay intact.
    op.execute(
        "DROP POLICY IF EXISTS owner_obligation_audit_insert "
        "ON owner_obligation_audit"
    )
    op.execute(f"""
        CREATE POLICY owner_obligation_audit_insert ON owner_obligation_audit
        FOR INSERT WITH CHECK ({OWNER_POLICY})
    """)
