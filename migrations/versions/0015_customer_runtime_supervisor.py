"""Register project-isolated customer bot runtime health and safe global discovery.

Revision ID: 0015_customer_runtime_supervisor
Revises: 0014_owner_setup_sessions

The SECURITY DEFINER discovery function exposes ONLY project ID, bot ID,
and registration timestamp to the trusted runtime role. No ciphertext,
customer information, or arbitrary project data is returned.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_customer_runtime_supervisor"
down_revision: str | None = "0014_owner_setup_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_PROJECT = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"

FUNCTION_NAME = "public.list_customer_bot_targets()"


def upgrade() -> None:
    op.create_table(
        "customer_bot_runtime_health",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id"),
        sa.CheckConstraint(
            "state IN ('setup_needed', 'queued', 'starting', 'running', 'stopped', 'error')",
            name="ck_customer_bot_runtime_health_state",
        ),
    )
    op.execute("ALTER TABLE customer_bot_runtime_health ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE customer_bot_runtime_health FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY customer_bot_runtime_health_tenant
        ON customer_bot_runtime_health
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

    # Function owner must be the trusted migrations role. Runtime role cannot
    # bypass RLS or fetch arbitrary BotInstance ciphertext directly.
    op.execute(
        """
        CREATE FUNCTION public.list_customer_bot_targets()
        RETURNS TABLE(
            project_id uuid,
            telegram_bot_id bigint,
            credential_version timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT b.project_id, b.telegram_bot_id, b.updated_at
            FROM public.bot_instances b
            JOIN public.projects p ON p.id = b.project_id
            WHERE p.status = 'active' AND b.status = 'registered'
              AND NOT EXISTS (
                  SELECT 1 FROM public.bot_instances other
                  WHERE other.project_id = b.project_id
                    AND other.status = 'registered'
                    AND other.id <> b.id
              )
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION_NAME} FROM PUBLIC")
    # Some installations create the runtime role before migrations (Compose),
    # while CI grants it only afterwards.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime') THEN
                GRANT EXECUTE ON FUNCTION {FUNCTION_NAME} TO membership_runtime;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}")
    op.execute("DROP POLICY IF EXISTS customer_bot_runtime_health_tenant "
               "ON customer_bot_runtime_health")
    op.execute("ALTER TABLE customer_bot_runtime_health NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE customer_bot_runtime_health DISABLE ROW LEVEL SECURITY")
    op.drop_table("customer_bot_runtime_health")
