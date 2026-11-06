"""Audited, owner-only merchant risk observation report.

Revision ID: 0030_platform_risk_review
Revises: 0029_merchant_commercial_guards

Adds only a fixed read-only report: no decisions, automatic bans, invoices,
payment changes, or new privileges on private merchant-risk tables.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0030_platform_risk_review"
down_revision: str | None = "0029_merchant_commercial_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.platform_merchant_risk_snapshot(
            p_since timestamptz, p_page integer, p_page_size integer
        ) RETURNS jsonb
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $report$
        DECLARE
            actor uuid := NULLIF(
                current_setting('app.current_user_id', true), ''
            )::uuid;
            result jsonb;
        BEGIN
            IF actor IS NULL OR NOT EXISTS (
                SELECT 1
                FROM public.telegram_users u
                JOIN public.platform_operators o
                  ON o.telegram_user_id = u.telegram_user_id
                WHERE u.id = actor AND o.role = 'owner'
            ) THEN
                RAISE EXCEPTION 'Platform access denied'
                    USING ERRCODE = '42501';
            END IF;
            IF p_since IS NULL OR p_since > now()
               OR p_page IS NULL OR p_page < 0 OR p_page > 100000
               OR p_page_size IS NULL OR p_page_size < 1
               OR p_page_size > 10 THEN
                RAISE EXCEPTION 'Invalid risk report parameters'
                    USING ERRCODE = '22023';
            END IF;

            INSERT INTO public.platform_report_audit(
                actor_user_id, report_kind, period_since
            ) VALUES (actor, 'merchant_risk_review', p_since);

            WITH chosen AS MATERIALIZED (
                SELECT risk.id, risk.project_id,
                       risk.telegram_chat_id, risk.risk_kind,
                       risk.observed_at
                FROM public.merchant_risk_observations risk
                WHERE risk.observed_at >= p_since
                  AND risk.risk_kind = 'historical_chat_other_owner'
            ),
            page_rows AS (
                SELECT o.*
                FROM chosen o
                ORDER BY o.observed_at DESC, o.id DESC
                LIMIT p_page_size OFFSET p_page * p_page_size
            )
            SELECT jsonb_build_object(
                'total', (SELECT count(*) FROM chosen),
                'pages', (
                    SELECT ceil(count(*)::numeric / p_page_size)::integer
                    FROM chosen
                ),
                'observations', (
                    SELECT COALESCE(jsonb_agg(
                        jsonb_build_object(
                            'chat_id', item.telegram_chat_id,
                            'project_name', COALESCE((
                                SELECT p.name
                                FROM public.projects p
                                WHERE p.id = item.project_id
                            ), 'Archived connection'),
                            'channel_name', COALESCE((
                                SELECT r.name FROM public.resources r
                                WHERE r.project_id = item.project_id
                                  AND r.external_id = item.telegram_chat_id
                                ORDER BY r.created_at DESC, r.id DESC
                                LIMIT 1
                            ), 'Channel unavailable'),
                            'kind', item.risk_kind,
                            'observed_at', item.observed_at
                        )
                        ORDER BY item.observed_at DESC, item.id DESC
                    ), '[]'::jsonb)
                    FROM page_rows item
                )
            ) INTO result;
            RETURN result;
        END;
        $report$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.platform_merchant_risk_snapshot(timestamptz, integer, integer)
        FROM PUBLIC
    """)
    op.execute("""
        DO $grant$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime'
            ) THEN
                GRANT EXECUTE ON FUNCTION
                public.platform_merchant_risk_snapshot(
                    timestamptz, integer, integer
                ) TO membership_runtime;
            END IF;
        END
        $grant$
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS
        public.platform_merchant_risk_snapshot(timestamptz, integer, integer)
    """)
