"""Project-scoped, read-only owner termination timestamps for Stars recovery.

Revision ID: 0058_stars_owner_history_scope
Revises: 0057_subscription_stars

The owner audit table remains owner-only under FORCE RLS. Verified payment
recovery has no owner user context; this narrow SECURITY DEFINER function
returns ONLY the termination timestamps of one in-scope subscription.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0058_stars_owner_history_scope"
down_revision: str | None = "0057_subscription_stars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.subscription_owner_end_times(
            p_project uuid, p_subscription uuid
        ) RETURNS timestamptz[]
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
            result timestamptz[];
        BEGIN
            IF p_project IS NULL OR p_subscription IS NULL
               OR p_project IS DISTINCT FROM
                  NULLIF(current_setting('app.current_project_id',true),'')::uuid
               OR NOT EXISTS (
                   SELECT 1 FROM public.subscriptions s
                   WHERE s.project_id=p_project AND s.id=p_subscription
               )
            THEN
                RAISE EXCEPTION 'Subscription termination scope mismatch'
                    USING ERRCODE='42501';
            END IF;
            SELECT coalesce(
                array_agg(a.created_at ORDER BY a.created_at),
                ARRAY[]::timestamptz[]
            ) INTO result
            FROM public.owner_obligation_audit a
            WHERE a.project_id=p_project
              AND a.target_id=p_subscription
              AND a.action='subscription_end';
            RETURN result;
        END;
        $f$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
            public.subscription_owner_end_times(uuid,uuid)
        FROM PUBLIC
    """)
    op.execute("""
        DO $grant$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
            ) THEN
                GRANT EXECUTE ON FUNCTION
                    public.subscription_owner_end_times(uuid,uuid)
                TO membership_runtime;
            END IF;
        END;
        $grant$
    """)


def downgrade() -> None:
    op.execute("""
        REVOKE ALL ON FUNCTION public.subscription_owner_end_times(uuid,uuid)
        FROM membership_runtime
    """)
    op.execute("""
        DROP FUNCTION IF EXISTS public.subscription_owner_end_times(uuid,uuid)
    """)
