"""Read-only, account-authorized ViMi Pay bill summary by sale category.

Revision ID: 0083_saas_unified_bill_summary
Revises: 0082_saas_remove_test_void

The statement never issues/voids an invoice, settles money or changes buyer access.
Native gross is grouped by currency: never add RUB, XTR and crypto together.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0083_saas_unified_bill_summary"
down_revision: str | None = "0082_saas_remove_test_void"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE FUNCTION public.platform_saas_merchant_bill_summary(
      p_month date, p_project uuid DEFAULT NULL
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE
      actor uuid;
      beginning timestamptz;
      ending timestamptz;
      turnover jsonb;
      service_due numeric;
      commission_due numeric;
      unpaid_count integer;
      first_invoice uuid;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      IF actor IS NULL OR p_month IS NULL
         OR extract(day FROM p_month)<>1
         OR (p_project IS NOT NULL AND NOT EXISTS(
           SELECT 1 FROM public.projects p
            WHERE p.id=p_project AND p.owner_user_id=actor
         )) THEN
        RAISE EXCEPTION 'Unverified ViMi Pay account/month'
          USING ERRCODE='42501';
      END IF;
      beginning:=p_month::timestamp AT TIME ZONE 'Europe/Moscow';
      ending:=(p_month::timestamp+interval '1 month')
                AT TIME ZONE 'Europe/Moscow';
      -- Gross sales are factual completed purchases, not necessarily
      -- billable commissions. Eligibility and refunds come from the
      -- canonical unified monthly preview / issued-invoice ledger.
      WITH sales AS (
        SELECT 'subscription'::text AS kind,
               p.currency_code::text AS currency,
               p.final_amount::numeric AS gross
          FROM public.payments p
         WHERE p.approved_at>=beginning AND p.approved_at<ending
           AND (p_project IS NULL OR p.project_id=p_project)
           AND EXISTS(
             SELECT 1 FROM public.merchant_project_periods h
              WHERE h.owner_user_id=actor AND h.project_id=p.project_id
                AND h.started_at<=p.approved_at
                AND (h.ended_at IS NULL OR h.ended_at>p.approved_at)
           )
        UNION ALL
        SELECT 'file',f.currency_code::text,f.amount::numeric
          FROM public.file_sale_manual_orders f
         WHERE f.status='approved' AND f.paid_at>=beginning
           AND f.paid_at<ending
           AND (p_project IS NULL OR f.project_id=p_project)
           AND EXISTS(
             SELECT 1 FROM public.merchant_project_periods h
              WHERE h.owner_user_id=actor AND h.project_id=f.project_id
                AND h.started_at<=f.paid_at
                AND (h.ended_at IS NULL OR h.ended_at>f.paid_at)
           )
        UNION ALL
        SELECT CASE WHEN s.source_kind='subscription_stars'
                    THEN 'subscription' ELSE 'file' END,
               'XTR',s.stars_amount::numeric
          FROM public.platform_saas_star_sales s
         WHERE s.owner_user_id=actor
           AND s.paid_at>=beginning AND s.paid_at<ending
           AND (p_project IS NULL OR s.project_id=p_project)
        UNION ALL
        SELECT CASE WHEN s.source_kind='subscription_crypto'
                    THEN 'subscription' ELSE 'file' END,
               s.currency_code::text,s.original_amount::numeric
          FROM public.platform_saas_crypto_sales s
         WHERE s.owner_user_id=actor
           AND s.paid_at>=beginning AND s.paid_at<ending
           AND (p_project IS NULL OR s.project_id=p_project)
      ),
      grouped AS (
        SELECT kind,currency,count(*)::integer AS sold,
               sum(gross)::numeric AS gross
          FROM sales
         GROUP BY kind,currency
      )
      SELECT coalesce(jsonb_agg(jsonb_build_object(
        'kind',kind,'currency',currency,'sold',sold,
        'gross',gross::text
      ) ORDER BY kind,currency),'[]'::jsonb)
        INTO turnover FROM grouped;
      -- Canonical v2_remaining includes all authenticated receipts and
      -- provider-verified applied refund credits. Never sum issue totals
      -- as though prior partial payments had not happened.
      WITH liabilities AS (
        SELECT i.id,i.kind,i.due_at,
               public.platform_saas_v2_remaining(i.id) AS remaining
          FROM public.platform_saas_invoices i
         WHERE i.owner_user_id=actor AND i.status='issued'
      )
      SELECT
        coalesce(sum(remaining) FILTER(WHERE kind='prepayment'),0),
        coalesce(sum(remaining) FILTER(WHERE kind='commission'),0),
        count(*) FILTER(WHERE remaining>0)::integer,
        (array_agg(id ORDER BY due_at,id)
          FILTER(WHERE remaining>0))[1]
        INTO service_due,commission_due,unpaid_count,first_invoice
        FROM liabilities;
      RETURN jsonb_build_object(
        'month',p_month,'project_id',p_project,'turnover',turnover,
        'service_due_rub',service_due::text,
        'commission_due_rub',commission_due::text,
        'total_due_rub',(service_due+commission_due)::text,
        'unpaid_count',unpaid_count,'first_invoice_id',first_invoice
      );
    END; $f$
    """)
    op.execute("""
    REVOKE ALL ON FUNCTION
      public.platform_saas_merchant_bill_summary(date,uuid) FROM PUBLIC
    """)
    op.execute("""
    DO $grant$
    BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
      THEN
        GRANT EXECUTE ON FUNCTION
          public.platform_saas_merchant_bill_summary(date,uuid)
          TO membership_runtime;
      END IF;
    END $grant$
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_merchant_bill_summary(date,uuid)"
    )
