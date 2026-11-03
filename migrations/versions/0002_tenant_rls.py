"""enable tenant row-level security

Revision ID: 0002_tenant_rls
Revises: 0001_tenant_core
Create Date: 2026-11-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_tenant_rls"
down_revision: str | None = "0001_tenant_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"
CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("ALTER TABLE projects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE projects FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_members ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_members FORCE ROW LEVEL SECURITY")

    op.execute(
        f"""
        CREATE POLICY project_members_select_policy ON project_members
        FOR SELECT
        USING (
            user_id = {CURRENT_USER}
            OR (
                {CURRENT_PROJECT} IS NOT NULL
                AND project_id = {CURRENT_PROJECT}
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY project_members_insert_policy ON project_members
        FOR INSERT
        WITH CHECK (
            {CURRENT_PROJECT} IS NOT NULL
            AND project_id = {CURRENT_PROJECT}
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY project_members_update_policy ON project_members
        FOR UPDATE
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
    op.execute(
        f"""
        CREATE POLICY project_members_delete_policy ON project_members
        FOR DELETE
        USING (
            {CURRENT_PROJECT} IS NOT NULL
            AND project_id = {CURRENT_PROJECT}
        )
        """
    )

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
        USING ({project_access})
        """
    )
    op.execute(
        f"""
        CREATE POLICY projects_insert_policy ON projects
        FOR INSERT
        WITH CHECK (owner_user_id = {CURRENT_USER})
        """
    )
    op.execute(
        f"""
        CREATE POLICY projects_update_policy ON projects
        FOR UPDATE
        USING (
            id = {CURRENT_PROJECT}
            AND ({project_access})
        )
        WITH CHECK (id = {CURRENT_PROJECT})
        """
    )
    op.execute(
        f"""
        CREATE POLICY projects_delete_policy ON projects
        FOR DELETE
        USING (
            id = {CURRENT_PROJECT}
            AND owner_user_id = {CURRENT_USER}
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS projects_delete_policy ON projects")
    op.execute("DROP POLICY IF EXISTS projects_update_policy ON projects")
    op.execute("DROP POLICY IF EXISTS projects_insert_policy ON projects")
    op.execute("DROP POLICY IF EXISTS projects_select_policy ON projects")
    op.execute("DROP POLICY IF EXISTS project_members_delete_policy ON project_members")
    op.execute("DROP POLICY IF EXISTS project_members_update_policy ON project_members")
    op.execute("DROP POLICY IF EXISTS project_members_insert_policy ON project_members")
    op.execute("DROP POLICY IF EXISTS project_members_select_policy ON project_members")
    op.execute("ALTER TABLE project_members NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_members DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE projects NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE projects DISABLE ROW LEVEL SECURITY")
