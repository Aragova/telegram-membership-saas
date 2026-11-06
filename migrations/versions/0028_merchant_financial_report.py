"""Read-only merchant financial continuity reporting over immutable payment events.

Revision ID: 0028_merchant_financial_report
Revises: 0027_channel_reuse_guard

A platform-only, audited report. No payments, projects or merchant IDs change.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0028_merchant_financial_report"
down_revision: str | None = "0027_channel_reuse_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.platform_merchant_finances(
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
                RAISE EXCEPTION 'Invalid merchant report parameters'
                    USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.platform_report_audit(
                actor_user_id, report_kind, period_since
            ) VALUES (actor, 'merchant_finances', p_since);

            WITH approved AS MATERIALIZED (
                -- One immutable original Payment UUID counts once,
                -- even if a malformed duplicate metering row is present.
                SELECT DISTINCT ON (e.source_id)
                       e.source_id, e.project_id, e.currency_code,
                       e.amount, e.occurred_at
                FROM public.platform_usage_events e
                WHERE e.event_type = 'payment_approved'
                  AND e.occurred_at >= p_since
                ORDER BY e.source_id, e.id
            ),
            attributed AS MATERIALIZED (
                SELECT e.*, period.merchant_id
                FROM approved e
                LEFT JOIN LATERAL (
                    -- Historical attribution survives project deletion.
                    -- Never join by Telegram Chat ID or merchant owner alone.
                    SELECT mp.merchant_id
                    FROM public.merchant_project_periods mp
                    WHERE mp.project_id = e.project_id
                      AND e.occurred_at >= mp.started_at
                      AND (mp.ended_at IS NULL OR e.occurred_at <= mp.ended_at)
                    ORDER BY mp.started_at DESC, mp.id DESC
                    LIMIT 1
                ) period ON TRUE
            ),
            currencies AS (
                SELECT a.merchant_id, a.currency_code,
                       count(*) AS payments, sum(a.amount) AS amount
                FROM attributed a
                WHERE a.merchant_id IS NOT NULL
                GROUP BY a.merchant_id, a.currency_code
            ),
            businesses AS MATERIALIZED (
                SELECT c.merchant_id, sum(c.payments) AS payments,
                       jsonb_agg(
                           jsonb_build_object(
                               'currency', c.currency_code,
                               'count', c.payments,
                               'amount', c.amount::text
                           ) ORDER BY c.currency_code
                       ) AS currencies
                FROM currencies c
                GROUP BY c.merchant_id
            ),
            page_rows AS (
                SELECT b.*
                FROM businesses b
                ORDER BY b.payments DESC, b.merchant_id
                LIMIT p_page_size OFFSET p_page * p_page_size
            )
            SELECT jsonb_build_object(
                'merchant_count', (SELECT count(*) FROM businesses),
                'pages', (
                    SELECT ceil(count(*)::numeric / p_page_size)::integer
                    FROM businesses
                ),
                'unmapped_payments', (
                    SELECT count(*) FROM attributed WHERE merchant_id IS NULL
                ),
                'merchants', (
                    SELECT COALESCE(jsonb_agg(
                        jsonb_build_object(
                            'merchant_id', pg.merchant_id::text,
                            'label', COALESCE((
                                SELECT p.name FROM public.projects p
                                WHERE p.merchant_id = pg.merchant_id
                                ORDER BY p.created_at DESC, p.id DESC
                                LIMIT 1
                            ), 'Archived connection'),
                            'payments', pg.payments,
                            'currencies', pg.currencies
                        )
                        ORDER BY pg.payments DESC, pg.merchant_id
                    ), '[]'::jsonb)
                    FROM page_rows pg
                )
            ) INTO result;
            RETURN result;
        END;
        $report$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.platform_merchant_finances(timestamptz, integer, integer)
        FROM PUBLIC
    """)
    op.execute("""
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime')
            THEN
                GRANT EXECUTE ON FUNCTION
                public.platform_merchant_finances(timestamptz, integer, integer)
                TO membership_runtime;
            END IF;
        END
        $grant$
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS
        public.platform_merchant_finances(timestamptz, integer, integer)
    """)
