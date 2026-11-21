"""Owner-only native-currency sales preview for all six confirmed ViMi Pay rails.

Revision ID: 0104_saas_native_sales
Revises: 0103_telegram_degraded_runtime

Presentation-only, read-only SQL. It never creates debt, converts FX,
charges commissions or changes any purchase/invoice/receipt.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0104_saas_native_sales"
down_revision: str | None = "0103_telegram_degraded_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.platform_saas_bot_native_sales_preview(
            p_project uuid, p_month date
        ) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
          SET search_path = pg_catalog, public AS $fn$
        DECLARE
          actor uuid;
          month_from timestamptz;
          month_to timestamptz;
          grouped jsonb;
        BEGIN
          actor := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
          IF actor IS NULL OR p_project IS NULL OR p_month IS NULL
             OR extract(day FROM p_month) <> 1
             OR NOT EXISTS(
               SELECT 1 FROM public.projects p
               WHERE p.id = p_project AND p.owner_user_id = actor
             )
          THEN
            RAISE EXCEPTION 'Bot owner and first month day required'
              USING ERRCODE = '42501';
          END IF;
          month_from := p_month::timestamp AT TIME ZONE 'Europe/Moscow';
          month_to := (p_month::timestamp + interval '1 month')
                          AT TIME ZONE 'Europe/Moscow';

          WITH raw_sales AS (
            SELECT 'subscription'::text AS kind,
                   p.currency_code::text AS currency,
                   p.final_amount::numeric AS amount
            FROM public.payments p
            WHERE p.project_id = p_project
              AND p.approved_at >= month_from
              AND p.approved_at < month_to
              AND EXISTS(
                SELECT 1 FROM public.merchant_project_periods history
                WHERE history.project_id = p.project_id
                  AND history.owner_user_id = actor
                  AND history.started_at <= p.approved_at
                  AND (history.ended_at IS NULL
                       OR history.ended_at > p.approved_at)
              )
            UNION ALL
            SELECT 'file_manual'::text, f.currency_code::text,
                   f.amount::numeric
            FROM public.file_sale_manual_orders f
            WHERE f.project_id = p_project
              AND f.status = 'approved'
              AND f.paid_at >= month_from AND f.paid_at < month_to
              AND EXISTS(
                SELECT 1 FROM public.merchant_project_periods history
                WHERE history.project_id = f.project_id
                  AND history.owner_user_id = actor
                  AND history.started_at <= f.paid_at
                  AND (history.ended_at IS NULL
                       OR history.ended_at > f.paid_at)
              )
            UNION ALL
            SELECT s.source_kind::text, 'XTR'::text,
                   s.stars_amount::numeric
            FROM public.platform_saas_star_sales s
            WHERE s.project_id = p_project AND s.owner_user_id = actor
              AND s.paid_at >= month_from AND s.paid_at < month_to
            UNION ALL
            -- Telegram confirmed Stars payments can precede monthly reward
            -- capture (a reward quote may be missing). Include those native
            -- purchases once, without inventing a 5% invoice.
            SELECT 'file_stars'::text, 'XTR'::text,
                   f.stars_amount::numeric
            FROM public.file_sale_purchases f
            WHERE f.project_id = p_project AND f.status = 'paid'
              AND f.telegram_charge_id IS NOT NULL
              AND f.paid_at >= month_from AND f.paid_at < month_to
              AND EXISTS(
                SELECT 1 FROM public.merchant_project_periods history
                WHERE history.project_id = f.project_id
                  AND history.owner_user_id = actor
                  AND history.started_at <= f.paid_at
                  AND (history.ended_at IS NULL
                       OR history.ended_at > f.paid_at)
              )
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_saas_star_sales captured
                WHERE captured.source_kind = 'file_stars'
                  AND captured.source_id = f.id
              )
            UNION ALL
            SELECT 'subscription_stars'::text, 'XTR'::text,
                   o.stars_amount::numeric
            FROM public.subscription_star_orders o
            WHERE o.project_id = p_project AND o.status = 'paid'
              AND o.telegram_charge_id IS NOT NULL
              AND o.paid_at >= month_from AND o.paid_at < month_to
              AND EXISTS(
                SELECT 1 FROM public.merchant_project_periods history
                WHERE history.project_id = o.project_id
                  AND history.owner_user_id = actor
                  AND history.started_at <= o.paid_at
                  AND (history.ended_at IS NULL
                       OR history.ended_at > o.paid_at)
              )
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_saas_star_sales captured
                WHERE captured.source_kind = 'subscription_stars'
                  AND captured.source_id = o.id
              )
            UNION ALL
            -- Crypto Pay may verify a payment BEFORE sale-day USD valuation
            -- becomes available. The paid MAINNET invoice is the authority
            -- for native turnover, not the deferred commission evidence.
            -- Read it only once, including already captured paid invoices.
            SELECT CASE WHEN i.kind = 'subscription'
                        THEN 'subscription_crypto'::text
                        ELSE 'file_crypto'::text END,
                   i.currency_code::text, i.amount::numeric
            FROM public.crypto_pay_invoices i
            JOIN public.crypto_pay_apps a
              ON a.id = i.app_id AND a.project_id = i.project_id
            WHERE i.project_id = p_project AND i.status = 'paid'
              AND a.environment = 'mainnet'
              AND i.paid_at >= month_from AND i.paid_at < month_to
              AND EXISTS(
                SELECT 1 FROM public.merchant_project_periods history
                WHERE history.project_id = i.project_id
                  AND history.owner_user_id = actor
                  AND history.started_at <= i.paid_at
                  AND (history.ended_at IS NULL
                       OR history.ended_at > i.paid_at)
              )
          ),
          per_rail_currency AS (
            SELECT kind, currency, count(*)::integer AS sold,
                   sum(amount) AS gross
            FROM raw_sales GROUP BY kind, currency
          )
          SELECT coalesce(jsonb_agg(jsonb_build_object(
              'kind', kind, 'currency', currency,
              'sold', sold, 'gross', gross::text
          ) ORDER BY kind, currency), '[]'::jsonb)
          INTO grouped FROM per_rail_currency;

          RETURN jsonb_build_object(
            'project_id', p_project,
            'month', p_month,
            'rows', grouped,
            'preview_only', true
          );
        END; $fn$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.platform_saas_bot_native_sales_preview(uuid,date)
        FROM PUBLIC
    """)
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION
            public.platform_saas_bot_native_sales_preview(uuid,date)
            TO membership_runtime;
          END IF;
        END $grants$
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_bot_native_sales_preview(uuid,date)"
    )
