"""One bot, one billing statement. No owner-account balance aggregation.

Revision ID: 0083_saas_unified_bill_summary
Revises: 0082_saas_remove_test_void

The statement is read-only and uses ONLY canonical issued invoice lines,
verified receipts and applied credits. It never charges provisional turnover.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0083_saas_unified_bill_summary"
down_revision: str | None = "0082_saas_remove_test_void"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_merchant_bill_summary(
  p_month date,p_project uuid DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  actor uuid;
  bot_id uuid;
  bots integer;
  bill record;
  turnover jsonb;
  service_rub numeric:=0;
  subscriptions_rub numeric:=0;
  files_rub numeric:=0;
  remaining numeric:=0;
  received numeric:=0;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  IF actor IS NULL OR p_project IS NULL THEN
    RAISE EXCEPTION 'Select one owned ViMi Pay bot'
      USING ERRCODE='42501';
  END IF;
  IF NOT EXISTS(
    SELECT 1 FROM public.projects p
    WHERE p.id=p_project AND p.owner_user_id=actor
  ) THEN RAISE EXCEPTION 'Bot ownership required' USING ERRCODE='42501'; END IF;
  SELECT count(*),max(b.id::text)::uuid INTO bots,bot_id
  FROM public.bot_instances b
  WHERE b.project_id=p_project AND b.status='registered';
  IF bots<>1 THEN
    RAISE EXCEPTION 'One active bot per project required'
      USING ERRCODE='42501';
  END IF;
  SELECT i.* INTO bill
  FROM public.platform_saas_invoices i
  WHERE i.bot_instance_id=bot_id AND i.owner_user_id=actor
    AND i.status='issued' AND i.kind='prepayment'
  ORDER BY i.period_start DESC LIMIT 1;
  IF bill.id IS NULL THEN
    RETURN jsonb_build_object('bot_id',bot_id,'project_id',p_project,
      'invoice_id',NULL,'period_start',NULL,'period_end',NULL,
      'turnover','[]'::jsonb,'service_rub','0',
      'subscription_rub','0','file_rub','0','total_rub','0',
      'paid_rub','0','total_due_rub','0','first_period',true);
  END IF;
  SELECT
    coalesce(sum(l.charge_rub) FILTER(
      WHERE l.source_kind='bot_fee'),0),
    coalesce(sum(l.charge_rub) FILTER(
      WHERE l.source_kind IN ('subscription','subscription_stars',
                              'subscription_crypto')),0),
    coalesce(sum(l.charge_rub) FILTER(
      WHERE l.source_kind IN ('file_manual','file_stars','file_crypto')),0)
  INTO service_rub,subscriptions_rub,files_rub
  FROM public.platform_saas_invoice_lines l WHERE l.invoice_id=bill.id;
  WITH groups AS (
    SELECT CASE WHEN l.source_kind LIKE 'subscription%' THEN 'subscription'
                ELSE 'file' END AS kind,
           l.original_currency AS currency,
           count(*)::integer AS sold,sum(l.original_amount) AS gross
    FROM public.platform_saas_invoice_lines l
    WHERE l.invoice_id=bill.id AND l.source_kind<>'bot_fee'
    GROUP BY 1,2
  )
  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'kind',kind,'currency',currency,'sold',sold,'gross',gross::text
  ) ORDER BY kind,currency),'[]'::jsonb) INTO turnover FROM groups;
  remaining:=public.platform_saas_v2_remaining(bill.id);
  received:=bill.total_rub-remaining;
  RETURN jsonb_build_object(
    'bot_id',bot_id,'project_id',p_project,'invoice_id',bill.id,
    'period_start',bill.period_start,'period_end',bill.period_end,
    'turnover',turnover,'service_rub',service_rub::text,
    'subscription_rub',round(subscriptions_rub,2)::text,
    'file_rub',round(files_rub,2)::text,'total_rub',bill.total_rub::text,
    'paid_rub',received::text,'total_due_rub',remaining::text,
    'first_period',NOT EXISTS(
      SELECT 1 FROM public.platform_saas_invoices prior
      WHERE prior.bot_instance_id=bot_id AND prior.owner_user_id=actor
        AND prior.kind='prepayment' AND prior.status='issued'
        AND prior.period_end=bill.period_start
    )
  );
END; $f$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
      public.platform_saas_merchant_bill_summary(date,uuid) FROM PUBLIC
    """)
    op.execute("""
    DO $grants$
    BEGIN
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
      THEN GRANT EXECUTE ON FUNCTION
        public.platform_saas_merchant_bill_summary(date,uuid)
        TO membership_runtime;
      END IF;
    END $grants$
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_merchant_bill_summary(date,uuid)"
    )
