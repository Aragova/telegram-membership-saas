"""Read-only SaaS launch audit for verified creator-owned channels.

Revision ID: 0048_saas_prelaunch_readiness
Revises: 0047_saas_owner_payment_ui

No charges, policy switches, existing-access changes or financial writes.
Returns only operator-owned bot names and exemption verification status.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0048_saas_prelaunch_readiness"
down_revision: str | None = "0047_saas_owner_payment_ui"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Reuse the existing read-only platform-owner authorization. No general
    # tenant-level SELECT on the exemption ledger, bots or channel history.
    op.execute("""
        CREATE FUNCTION public.platform_saas_exemption_preflight()
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE snapshot jsonb;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          WITH scoped AS MATERIALIZED (
            SELECT b.username,
                   public.platform_saas_exemption_state(
                       p.id, b.id, p.merchant_id, p.owner_user_id,
                       clock_timestamp()
                   ) AS exemption_state
              FROM public.bot_instances b
              JOIN public.projects p ON p.id = b.project_id
              JOIN public.telegram_users u ON u.id = p.owner_user_id
              JOIN public.platform_operators po
                ON po.telegram_user_id = u.telegram_user_id
             WHERE po.role = 'owner' AND b.status = 'registered'
          ), totals AS (
            SELECT count(*) AS operator_bots,
                   count(*) FILTER (WHERE exemption_state='exempt')
                       AS verified,
                   count(*) FILTER (WHERE exemption_state<>'exempt')
                       AS pending
              FROM scoped
          )
          SELECT jsonb_build_object(
              'operator_bots', totals.operator_bots,
              'verified', totals.verified,
              'pending', totals.pending,
              'pending_bots', coalesce((
                  SELECT jsonb_agg(jsonb_build_object(
                      'username', p.username,
                      'state', p.exemption_state
                  ) ORDER BY p.username)
                    FROM (
                      SELECT username, exemption_state
                        FROM scoped
                       WHERE exemption_state <> 'exempt'
                       ORDER BY username LIMIT 8
                    ) p
              ), '[]'::jsonb)
          ) INTO snapshot FROM totals;
          RETURN snapshot;
        END;
        $function$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_exemption_preflight() FROM PUBLIC"
    )
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_exemption_preflight()
              TO membership_runtime;
          END IF;
        END $grants$
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_exemption_preflight()"
    )
