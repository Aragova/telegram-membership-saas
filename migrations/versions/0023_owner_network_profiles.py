"""Opt-in owner network profiles with strict owner-only RLS.

Revision ID: 0023_owner_network_profiles
Revises: 0022_detected_user_language
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0023_owner_network_profiles"
down_revision: str | None = "0022_detected_user_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "owner_network_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("auto_apply", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("owner_user_id", name="uq_network_profiles_owner"),
        sa.CheckConstraint("version >= 1", name="ck_network_profiles_version"),
    )
    op.create_index(
        "ix_owner_network_profiles_owner_user_id", "owner_network_profiles",
        ["owner_user_id"],
    )
    op.create_table(
        "project_profile_applications",
        sa.Column(
            "project_id", sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "owner_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "profile_id", sa.Uuid(),
            sa.ForeignKey("owner_network_profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('applied', 'failed')", name="ck_profile_applications_status",
        ),
        sa.CheckConstraint("profile_version >= 1", name="ck_profile_applications_version"),
    )
    op.create_index(
        "ix_project_profile_applications_owner_user_id",
        "project_profile_applications", ["owner_user_id"],
    )
    for table in ("owner_network_profiles", "project_profile_applications"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY network_profile_actor ON owner_network_profiles
        USING (owner_user_id = {ACTOR})
        WITH CHECK (owner_user_id = {ACTOR})
    """)
    op.execute(f"""
        CREATE POLICY network_application_actor ON project_profile_applications
        USING (
            owner_user_id = {ACTOR}
            AND EXISTS (
                SELECT 1 FROM projects p
                WHERE p.id = project_id AND p.owner_user_id = {ACTOR}
            )
        )
        WITH CHECK (
            owner_user_id = {ACTOR}
            AND EXISTS (
                SELECT 1 FROM projects p
                WHERE p.id = project_id AND p.owner_user_id = {ACTOR}
            )
            AND EXISTS (
                SELECT 1 FROM owner_network_profiles n
                WHERE n.id = profile_id AND n.owner_user_id = {ACTOR}
            )
        )
    """)
    # A trusted ADMIN of this one project may read ONLY the application
    # metadata (not the profile snapshot) to link draft plans after verifying
    # the channel. Profile writes remain restricted to the owner.
    op.execute(f"""
        CREATE POLICY network_application_project_admin_read
        ON project_profile_applications FOR SELECT
        USING (
            project_id = NULLIF(
                current_setting('app.current_project_id', true), ''
            )::uuid
            AND EXISTS (
                SELECT 1 FROM project_members pm
                WHERE pm.project_id = project_id
                  AND pm.user_id = {ACTOR}
                  AND pm.role = 'admin'
            )
        )
    """)
    # The staging runtime role already exists before this migration.
    # CI creates it afterwards and grants DML on ALL TABLES.
    op.execute("""
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime')
            THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON owner_network_profiles, project_profile_applications
                    TO membership_runtime;
            END IF;
        END
        $grant$
    """)


def downgrade() -> None:
    op.drop_table("project_profile_applications")
    op.drop_table("owner_network_profiles")
