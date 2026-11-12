"""One-account multi-rail monetary preview for the commercial billing v2 release.

Revision ID: 0066_commercial_v2
Revises: 0065_crypto_refund_credits

Preview-only until shared invoice source issuance and operator proof allocation
are implemented in this SAME release PR. No user is automatically charged.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0066_commercial_v2"
down_revision: str | None = "0065_crypto_refund_credits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Do not guess a payment's reward value, fiat quote or original merchant.
    # One monthly source identity is counted once regardless of file redelivery,
    # callback retries, bot transfers or price changes.
    op.execute("""
    CREATE FUNCTION public.platform_saas_unified_monthly_preview(
        p_owner uuid, p_month date, p_project uuid DEFAULT NULL
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
    DECLARE
      actor uuid;
      operator_allowed boolean;
      beginning timestamptz;
      ending timestamptz;
      launch_at timestamptz;
      trial_at timestamptz;
      summary jsonb;
      manual_count integer;
      stars_count integer;
      crypto_count integer;
      unrated_count integer;
      commission_rub numeric;
      estimated_usd numeric;
      star_credit_usd numeric;
      crypto_credit_usd numeric;
      missing_source_count integer;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      SELECT EXISTS(
        SELECT 1 FROM public.telegram_users u
        JOIN public.platform_operators po
          ON po.telegram_user_id=u.telegram_user_id
        WHERE u.id=actor AND po.role='owner'
      ) INTO operator_allowed;
      IF p_owner IS NULL OR p_month IS NULL
         OR extract(day FROM p_month)<>1
         OR (actor IS DISTINCT FROM p_owner AND NOT operator_allowed)
         OR (p_project IS NOT NULL AND NOT EXISTS(
           SELECT 1 FROM public.merchant_project_periods mp
           WHERE mp.project_id=p_project AND mp.owner_user_id=p_owner
         ))
      THEN
        RAISE EXCEPTION 'Unverified ViMi Pay commercial account/month'
          USING ERRCODE='42501';
      END IF;
      beginning:=p_month::timestamp AT TIME ZONE 'Europe/Moscow';
      ending:=(p_month::timestamp+interval '1 month')
                AT TIME ZONE 'Europe/Moscow';
      SELECT CASE WHEN b.enabled THEN b.effective_from ELSE NULL END
        INTO launch_at FROM public.platform_saas_billing_control b
       WHERE b.id=1;
      SELECT min(t.claimed_at) INTO trial_at
        FROM public.merchant_platform_trial_claims t
        WHERE t.owner_user_id=p_owner AND t.scope='owner'
          AND t.policy_key='vimi-saas-72h-v1';

      WITH manual AS (
        SELECT p.id AS source_id,'subscription'::text AS source_kind,
               p.project_id,p.approved_at AS paid_at,
               p.final_amount AS native_amount,p.currency_code AS currency
          FROM public.payments p
         WHERE p.approved_at>=beginning AND p.approved_at<ending
           AND (p_project IS NULL OR p.project_id=p_project)
        UNION ALL
        SELECT f.id,'file_manual',f.project_id,f.paid_at,
               f.amount,f.currency_code
          FROM public.file_sale_manual_orders f
         WHERE f.status='approved' AND f.paid_at>=beginning
           AND f.paid_at<ending
           AND (p_project IS NULL OR f.project_id=p_project)
      ),
      attributed AS (
        SELECT m.*,
          (SELECT count(*) FROM public.merchant_project_periods h
           WHERE h.project_id=m.project_id AND h.owner_user_id=p_owner
             AND h.started_at<=m.paid_at
             AND (h.ended_at IS NULL OR h.ended_at>m.paid_at)
          ) AS owner_periods,
          (SELECT count(*) FROM public.platform_usage_events e
           WHERE e.project_id=m.project_id
             AND e.event_type='bot_connected'
             AND e.occurred_at<=m.paid_at
             AND NOT EXISTS(
               SELECT 1 FROM public.platform_usage_events gone
               WHERE gone.project_id=m.project_id
                 AND gone.event_type='bot_removed'
                 AND gone.source_id=e.source_id
                 AND gone.occurred_at<=m.paid_at
             )
          ) AS bot_count,
          (SELECT max(e.source_id::text)::uuid
           FROM public.platform_usage_events e
           WHERE e.project_id=m.project_id
             AND e.event_type='bot_connected'
             AND e.occurred_at<=m.paid_at
             AND NOT EXISTS(
               SELECT 1 FROM public.platform_usage_events gone
               WHERE gone.project_id=m.project_id
                 AND gone.event_type='bot_removed'
                 AND gone.source_id=e.source_id
                 AND gone.occurred_at<=m.paid_at
             )
          ) AS original_bot,
          (SELECT min(h.merchant_id::text)::uuid
           FROM public.merchant_project_periods h
           WHERE h.project_id=m.project_id AND h.owner_user_id=p_owner
             AND h.started_at<=m.paid_at
             AND (h.ended_at IS NULL OR h.ended_at>m.paid_at)
          ) AS merchant_id
        FROM manual m
      ),
      all_sales AS (
        SELECT a.source_id,a.source_kind,a.project_id,a.paid_at,
               a.native_amount,a.currency,
               CASE WHEN a.owner_periods<>1 OR a.bot_count<>1
                  OR a.original_bot IS NULL OR a.merchant_id IS NULL
                 THEN false
                 WHEN launch_at IS NULL OR trial_at IS NULL
                   OR a.paid_at<greatest(
                     launch_at,trial_at+interval '72 hours'
                   ) THEN false
                 ELSE public.platform_saas_exemption_state(
                   a.project_id,a.original_bot,a.merchant_id,
                   p_owner,a.paid_at
                 )='not_exempt'
               END AS billable,
               CASE WHEN a.currency='RUB' THEN 1::numeric
                    ELSE rate.rub_per_unit END AS unit_rub
          FROM attributed a
          LEFT JOIN public.platform_saas_daily_fx_rates rate
            ON rate.currency_code=a.currency
           AND rate.rate_date=
                 (a.paid_at AT TIME ZONE 'Europe/Moscow')::date
           AND rate.source_name='CBR'
          WHERE a.owner_periods>0
        UNION ALL
        SELECT s.source_id,s.source_kind,s.project_id,s.paid_at,
               s.stars_amount::numeric,'XTR'::text,
               s.billing_eligible,
               s.usd_per_star*usd.rub_per_unit
        FROM public.platform_saas_star_sales s
        LEFT JOIN public.platform_saas_daily_fx_rates usd
          ON usd.currency_code='USD'
         AND usd.rate_date=
              (s.paid_at AT TIME ZONE 'Europe/Moscow')::date
        WHERE s.owner_user_id=p_owner
          AND s.paid_at>=beginning AND s.paid_at<ending
          AND (p_project IS NULL OR s.project_id=p_project)
        UNION ALL
        SELECT s.invoice_id,s.source_kind,s.project_id,s.paid_at,
               s.original_amount,s.currency_code,s.billing_eligible,
               s.usd_per_unit*usd.rub_per_unit
        FROM public.platform_saas_crypto_sales s
        LEFT JOIN public.platform_saas_daily_fx_rates usd
          ON usd.currency_code='USD'
         AND usd.rate_date=s.sale_day
        WHERE s.owner_user_id=p_owner
          AND s.paid_at>=beginning AND s.paid_at<ending
          AND (p_project IS NULL OR s.project_id=p_project)
      ),
      totals AS (
        SELECT source_kind, count(*) AS sales,
               count(*) FILTER(WHERE billable) AS eligible_sales,
               count(*) FILTER(WHERE billable AND unit_rub IS NULL) AS unrated,
               coalesce(sum(native_amount)
                   FILTER(WHERE billable),0) AS native_gross,
               coalesce(sum(native_amount*unit_rub*0.05)
                   FILTER(WHERE billable AND unit_rub IS NOT NULL),0)
                 AS commission_unrounded_rub
        FROM all_sales GROUP BY source_kind
      )
      SELECT
          coalesce(sum(sales) FILTER(
             WHERE source_kind IN ('subscription','file_manual')),0),
          coalesce(sum(sales) FILTER(
             WHERE source_kind IN ('file_stars','subscription_stars')),0),
          coalesce(sum(sales) FILTER(
             WHERE source_kind IN ('file_crypto','subscription_crypto')),0),
          coalesce(sum(unrated),0),
          round(coalesce(sum(commission_unrounded_rub),0),2),
          coalesce(jsonb_agg(jsonb_build_object(
             'kind',source_kind,'sold',sales,'billable',eligible_sales,
             'missing_rate',unrated,'native_gross',native_gross::text,
             'commission_rub',round(commission_unrounded_rub,2)::text
          ) ORDER BY source_kind),'[]'::jsonb)
      INTO manual_count,stars_count,crypto_count,unrated_count,
           commission_rub,summary
      FROM totals;
      SELECT coalesce(sum(c.commission_credit_usd),0) INTO star_credit_usd
      FROM (
        SELECT c.owner_user_id,c.project_id,c.commission_credit_usd,
               c.refunded_at FROM public.platform_saas_star_refund_credits c
        UNION ALL
        SELECT c.owner_user_id,c.project_id,c.commission_credit_usd,
               c.refunded_at
        FROM public.platform_saas_star_subscription_credits c
      ) c
      WHERE c.owner_user_id=p_owner
        AND c.refunded_at>=beginning AND c.refunded_at<ending
        AND (p_project IS NULL OR c.project_id=p_project);
      SELECT coalesce(sum(c.commission_credit_usd),0)
        INTO crypto_credit_usd
      FROM public.platform_saas_crypto_transfer_credits c
      WHERE c.owner_user_id=p_owner
        AND c.completed_at>=beginning AND c.completed_at<ending
        AND (p_project IS NULL OR c.project_id=p_project);
      SELECT count(*) INTO missing_source_count
      FROM public.crypto_pay_invoices i
      JOIN public.crypto_pay_apps app ON app.id=i.app_id
      JOIN public.projects p ON p.id=i.project_id
      WHERE i.status='paid' AND i.paid_at>=beginning
        AND i.paid_at<ending AND p.owner_user_id=p_owner
        AND (p_project IS NULL OR i.project_id=p_project)
        AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_crypto_sales sale
          WHERE sale.invoice_id=i.id
        );
      RETURN jsonb_build_object(
        'owner_user_id',p_owner,'project_id',p_project,
        'month',p_month,'billing_enabled',launch_at IS NOT NULL,
        'rollout_from',launch_at,'preview_only',true,
        'manual_sales',manual_count,'stars_sales',stars_count,
        'crypto_sales',crypto_count,
        'unrated_billable',unrated_count,
        'uncaptured_crypto',missing_source_count,
        'commission_rub',commission_rub::text,
        'rails',summary,'star_refund_credit_usd',star_credit_usd::text,
        'crypto_refund_credit_usd',crypto_credit_usd::text,
        'credits_applied',false,
        'issue_ready',launch_at IS NOT NULL AND unrated_count=0
                       AND missing_source_count=0
      );
    END;
    $f$
    """)
    op.execute("""
       REVOKE ALL ON FUNCTION
       public.platform_saas_unified_monthly_preview(uuid,date,uuid)
       FROM PUBLIC
    """)
    op.execute("""
      DO $grant$
      BEGIN
        IF EXISTS(SELECT 1 FROM pg_roles
          WHERE rolname='membership_runtime') THEN
          GRANT EXECUTE ON FUNCTION
           public.platform_saas_unified_monthly_preview(uuid,date,uuid)
          TO membership_runtime;
        END IF;
      END $grant$
    """)


def downgrade() -> None:
    op.execute("""
      DROP FUNCTION IF EXISTS
      public.platform_saas_unified_monthly_preview(uuid,date,uuid)
    """)
