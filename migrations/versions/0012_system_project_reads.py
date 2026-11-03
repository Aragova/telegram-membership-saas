"""allow trusted system scope to read its selected project

Revision ID: 0012_system_project_reads
Revises: 0011_telegram_access
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_system_project_reads"
down_revision: str | None = "0011_telegram_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"
CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def _create_original_policy() -> None:
    project_access = f"""
        owner_user_id = {CURRENT_USER}
        OR EXISTS (
            SELECT 1
            FROM project_members pm
            WHERE pm.project_id = projects.id
              AND pm.user_id = {CURRENT_USER}
        )
    """
    op.execute(
        f"""
        CREATE POLICY projects_select_policy ON projects
        FOR SELECT
        USING (
            ({project_access})
            AND (
                {CURRENT_PROJECT} IS NULL
                OR id = {CURRENT_PROJECT}
            )
        )
        """
    )


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS projects_select_policy ON projects")
    project_access = f"""
        owner_user_id = {CURRENT_USER}
        OR EXISTS (
            SELECT 1
            FROM project_members pm
            WHERE pm.project_id = projects.id
              AND pm.user_id = {CURRENT_USER}
        )
    """
    op.execute(
        f"""
        CREATE POLICY projects_select_policy ON projects
        FOR SELECT
        USING (
            (
                {CURRENT_USER} IS NULL
                AND {CURRENT_PROJECT} IS NOT NULL
                AND id = {CURRENT_PROJECT}
            )
            OR (
                ({project_access})
                AND (
                    {CURRENT_PROJECT} IS NULL
                    OR id = {CURRENT_PROJECT}
                )
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS projects_select_policy ON projects")
    _create_original_policy()
