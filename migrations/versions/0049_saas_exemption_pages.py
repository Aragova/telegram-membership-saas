"""Read-only, paginated creator-exemption audit, separate from bank details.

Revision ID: 0049_saas_exemption_pages
Revises: 0048_saas_prelaunch_readiness

No billing, trial, holds or exemption records are changed.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0049_saas_exemption_pages"
down_revision: str | None = "0048_saas_prelaunch_readiness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.platform_saas_exemption_preflight_page(
            p_page integer, p_size integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE
          actor uuid;
          snapshot jsonb;
        BEGIN
          actor := public.platform_saas_require_owner();
          IF p_page IS NULL OR p_page < 0 OR p_page > 100000
             OR p_size IS NULL OR p_size < 1 OR p_size > 10 THEN
            RAISE EXCEPTION 'Invalid creator exemption page'
              USING ERRCODE = '22023';
          END IF;
          WITH scoped AS MATERIALIZED (
            -- Current registered bots held by this authenticated operator.
            -- Being a project owner is not Telegram creator verification.
            SELECT b.id, b.username,
                   public.platform_saas_exemption_state(
                       p.id, b.id, p.merchant_id, p.owner_user_id,
                       clock_timestamp()
                   ) AS exemption_state
              FROM public.bot_instances b
              JOIN public.projects p ON p.id=b.project_id
             WHERE p.owner_user_id=actor AND b.status='registered'
          ), totals AS (
            SELECT count(*) AS operator_bots,
                   count(*) FILTER (WHERE exemption_state='exempt') AS verified,
                   count(*) FILTER (WHERE exemption_state<>'exempt') AS pending
              FROM scoped
          )
          SELECT jsonb_build_object(
            'operator_bots', totals.operator_bots,
            'verified', totals.verified,
            'pending', totals.pending,
            'page', p_page,
            'pages', greatest(1,ceil(totals.pending::numeric/p_size)::integer),
            'pending_bots', coalesce((
                SELECT jsonb_agg(jsonb_build_object(
                    'username', pending.username,
                    'state', pending.exemption_state
                ) ORDER BY pending.username, pending.id)
                FROM (
                  SELECT id,username,exemption_state FROM scoped
                   WHERE exemption_state <> 'exempt'
                   ORDER BY username,id
                   LIMIT p_size OFFSET p_page*p_size
                ) pending
            ), '[]'::jsonb)
          ) INTO snapshot FROM totals;
          RETURN snapshot;
        END;
        $fn$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_exemption_preflight_page(integer,integer) "
        "FROM PUBLIC"
    )
    op.execute("""
        DO $grant$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles
                     WHERE rolname='membership_runtime') THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_exemption_preflight_page(integer,integer)
            TO membership_runtime;
          END IF;
        END $grant$
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_exemption_preflight_page(integer,integer)"
    )
