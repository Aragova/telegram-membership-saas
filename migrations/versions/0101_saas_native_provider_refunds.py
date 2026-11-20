"""Immutable native USD refund evidence for platform provider settlements.

Revision ID: 0101_saas_native_provider_refunds
Revises: 0100_saas_native_gateway

A verified provider refund in USD is translated into the exact corresponding
book-RUB liability under the same invoice/charge lock and recorded separately
in immutable native evidence. Operator command is NOT a refund API.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0101_saas_native_provider_refunds"
down_revision: str | None = "0100_saas_native_gateway"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE public.platform_saas_gateway_refund_native_evidence(
  refund_id uuid PRIMARY KEY REFERENCES public.platform_saas_gateway_refunds(id),
  currency text NOT NULL CHECK(currency IN ('RUB','USD')),
  amount_native numeric(38,2) NOT NULL CHECK(amount_native>0),
  amount_book_rub numeric(38,2) NOT NULL CHECK(amount_book_rub>0),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
    """)
    op.execute("""
ALTER TABLE public.platform_saas_gateway_refund_native_evidence
 ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
ALTER TABLE public.platform_saas_gateway_refund_native_evidence
 FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
REVOKE ALL ON TABLE public.platform_saas_gateway_refund_native_evidence
 FROM PUBLIC
    """)
    op.execute("""
DO $r$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
  REVOKE ALL ON TABLE public.platform_saas_gateway_refund_native_evidence
   FROM membership_runtime;
 END IF;
END $r$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_native_refund_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
BEGIN
 RAISE EXCEPTION 'Native provider refund evidence is immutable'
  USING ERRCODE='42501';
END; $f$
    """)
    op.execute("""
CREATE TRIGGER trg_saas_gateway_native_refund_immutable
BEFORE UPDATE OR DELETE ON public.platform_saas_gateway_refund_native_evidence
FOR EACH ROW EXECUTE FUNCTION
 public.platform_saas_gateway_native_refund_immutable()
    """)
    op.execute("""
REVOKE ALL ON FUNCTION
 public.platform_saas_gateway_native_refund_immutable() FROM PUBLIC
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_gateway_operator_refund(
 p_provider text,p_provider_invoice_id text,p_refund_id text,
 p_amount_rub numeric
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE pay_ccy text; target_order uuid; inserted boolean; verified record;
BEGIN
 PERFORM public.platform_saas_require_owner();
 SELECT o.id,o.pay_currency INTO target_order,pay_ccy
 FROM public.platform_saas_gateway_charges c
 JOIN public.platform_saas_gateway_orders o ON o.id=c.order_id
 WHERE c.provider=p_provider AND c.provider_invoice_id=p_provider_invoice_id;
 IF pay_ccy='USD' THEN
   RAISE EXCEPTION 'Use native USD amount for provider refund'
      USING ERRCODE='22023';
 END IF;
 IF target_order IS NULL THEN
   RAISE EXCEPTION 'No verified provider charge' USING ERRCODE='42501';
 END IF;
 inserted:=public.platform_saas_gateway_record_refund(
  target_order,p_provider,p_provider_invoice_id,
  p_refund_id,p_amount_rub,clock_timestamp()
 );
 SELECT r.* INTO verified FROM public.platform_saas_gateway_refunds r
 WHERE r.provider=p_provider AND r.provider_refund_id=p_refund_id;
 RETURN jsonb_build_object(
  'recorded',inserted,'amount_rub',verified.amount_rub::text,
  'credit_reversed_rub',verified.reversed_rub::text
 );
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_operator_refund_native(
 p_provider text,p_provider_invoice_id text,p_refund_id text,
 p_amount_native numeric,p_currency text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE o record; c record; prev_native numeric; prev_book numeric;
        refunded_native numeric; refunded_book numeric;
        book_amount numeric; inserted boolean; verified record; evidence record;
BEGIN
 PERFORM public.platform_saas_require_owner();
 IF p_provider NOT IN ('crypto','lava') OR p_currency<>'USD'
   OR p_provider_invoice_id IS NULL
   OR length(p_provider_invoice_id) NOT BETWEEN 1 AND 128
   OR p_refund_id IS NULL OR length(p_refund_id) NOT BETWEEN 1 AND 128
   OR p_amount_native IS NULL OR p_amount_native<=0
   OR p_amount_native<>round(p_amount_native,2) THEN
  RAISE EXCEPTION 'Invalid native USD refund evidence'
   USING ERRCODE='22023';
 END IF;
 SELECT o.* INTO o FROM public.platform_saas_gateway_orders o
 WHERE o.provider=p_provider
   AND o.provider_invoice_id=p_provider_invoice_id;
 IF o.id IS NULL OR o.pay_currency<>'USD' THEN
  RAISE EXCEPTION 'Original USD paid provider order required'
   USING ERRCODE='42501';
 END IF;
 -- Check and book under original invoice lock, the same ordering as
 -- record_refund. This serializes with other refund evidence.
 PERFORM 1 FROM public.platform_saas_invoices i
 WHERE i.id=o.invoice_id FOR UPDATE;
 SELECT * INTO c FROM public.platform_saas_gateway_charges c
 WHERE c.order_id=o.id AND c.provider=p_provider
   AND c.provider_invoice_id=p_provider_invoice_id FOR UPDATE;
 IF c.id IS NULL THEN
  RAISE EXCEPTION 'Original paid provider receipt required'
   USING ERRCODE='42501';
 END IF;
 SELECT r.*,n.amount_native,n.currency AS native_ccy INTO verified
 FROM public.platform_saas_gateway_refunds r
 LEFT JOIN public.platform_saas_gateway_refund_native_evidence n
  ON n.refund_id=r.id
 WHERE r.provider=p_provider AND r.provider_refund_id=p_refund_id;
 IF verified.id IS NOT NULL THEN
  IF verified.charge_id<>c.id OR verified.amount_native IS DISTINCT FROM
    p_amount_native OR verified.native_ccy IS DISTINCT FROM p_currency THEN
   RAISE EXCEPTION 'Conflicting native provider refund replay'
     USING ERRCODE='23505';
  END IF;
  RETURN jsonb_build_object(
    'recorded',false,'currency',p_currency,
    'amount_native',verified.amount_native::text,
    'amount_rub',verified.amount_rub::text,
    'credit_reversed_rub',verified.reversed_rub::text
  );
 END IF;
 SELECT coalesce(sum(n.amount_native),0),coalesce(sum(n.amount_book_rub),0)
 INTO prev_native,prev_book
 FROM public.platform_saas_gateway_refunds r
 JOIN public.platform_saas_gateway_refund_native_evidence n
   ON n.refund_id=r.id
 WHERE r.charge_id=c.id;
 SELECT coalesce(sum(r.amount_rub),0) INTO refunded_book
 FROM public.platform_saas_gateway_refunds r WHERE r.charge_id=c.id;
 -- Do not guess how old RUB-only evidence maps to the provider's USD
 -- refunds. Operator reconciliation is required before proceeding.
 IF refunded_book<>prev_book THEN
  RAISE EXCEPTION 'Legacy refund requires native reconciliation'
    USING ERRCODE='42501';
 END IF;
 IF prev_native+p_amount_native>o.pay_amount THEN
  RAISE EXCEPTION 'USD refund exceeds original provider charge'
    USING ERRCODE='23514';
 END IF;
 IF prev_native+p_amount_native=o.pay_amount THEN
  book_amount:=c.amount_rub-prev_book;
 ELSE
  book_amount:=round(c.amount_rub*p_amount_native/o.pay_amount,2);
 END IF;
 IF book_amount<=0 OR book_amount>c.amount_rub-prev_book THEN
  RAISE EXCEPTION 'Native refund cannot be allocated exactly'
   USING ERRCODE='23514';
 END IF;
 inserted:=public.platform_saas_gateway_record_refund(
  o.id,p_provider,p_provider_invoice_id,p_refund_id,
  book_amount,clock_timestamp()
 );
 IF inserted IS DISTINCT FROM true THEN
   RAISE EXCEPTION 'Unexpected native refund replay' USING ERRCODE='23505';
 END IF;
 SELECT r.* INTO verified FROM public.platform_saas_gateway_refunds r
 WHERE r.provider=p_provider AND r.provider_refund_id=p_refund_id;
 INSERT INTO public.platform_saas_gateway_refund_native_evidence(
   refund_id,currency,amount_native,amount_book_rub)
 VALUES(verified.id,p_currency,p_amount_native,book_amount);
 RETURN jsonb_build_object(
   'recorded',true,'currency',p_currency,
   'amount_native',p_amount_native::text,
   'amount_rub',verified.amount_rub::text,
   'credit_reversed_rub',verified.reversed_rub::text
 );
END; $f$
    """)
    op.execute("""
REVOKE ALL ON FUNCTION
 public.platform_saas_gateway_operator_refund_native(
  text,text,text,numeric,text) FROM PUBLIC
    """)
    op.execute("""
DO $r$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
  GRANT EXECUTE ON FUNCTION
    public.platform_saas_gateway_operator_refund_native(
     text,text,text,numeric,text)
  TO membership_runtime;
 END IF;
END $r$
    """)

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
  service_native numeric:=0;
  service_currency text:='RUB';
  service_fx numeric;
  bill_total_usd numeric;
  bill_due_usd numeric;
  paid_book numeric;
  remaining_service numeric;
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
      'due_at',NULL,
      'upcoming_at',CASE
        WHEN (SELECT c.enabled FROM public.platform_saas_billing_control c
              WHERE c.id=1) THEN public.platform_saas_billable_after(actor)
        ELSE NULL END,
      'hold_state',public.platform_saas_owner_overdue_state(
        actor,clock_timestamp()
      ),
      'turnover','[]'::jsonb,'service_rub','0',
      'service_native','0','service_currency','RUB',
      'subscription_rub','0','file_rub','0','total_rub','0',
      'paid_rub','0','credit_rub','0',
      'total_due_rub','0','first_period',true);
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
  SELECT l.original_amount,l.original_currency,l.rub_per_unit
  INTO service_native,service_currency,service_fx
  FROM public.platform_saas_invoice_lines l
  WHERE l.invoice_id=bill.id AND l.source_kind='bot_fee'
  LIMIT 1;
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
  IF service_currency='USD' THEN
    IF service_native<=0 OR service_rub<=0 OR service_fx<=0
       OR service_fx IS NULL THEN
      RAISE EXCEPTION 'Invalid original USD service snapshot'
        USING ERRCODE='23514';
    END IF;
    -- Preserve the immutable $5 / historical $3 fee and the source-day
    -- CBR valuation of the sales commission, never current FX.
    bill_total_usd:=ceil((
      service_native+(bill.total_rub-service_rub)/service_fx)*100
    )/100;
    paid_book:=bill.total_rub-remaining;
    remaining_service:=greatest(0,least(
      remaining,service_rub,service_rub-paid_book));
    bill_due_usd:=CASE WHEN remaining=0 THEN 0 ELSE ceil((
      service_native*remaining_service/service_rub
      +(remaining-remaining_service)/service_fx)*100
    )/100 END;
  END IF;
  SELECT coalesce(sum(r.amount_rub),0) INTO received
    FROM public.platform_saas_receipts r WHERE r.invoice_id=bill.id;
  received:=received+coalesce((
    SELECT sum(a.allocated_rub)
    FROM public.platform_saas_payment_allocations_v2 a
    WHERE a.invoice_id=bill.id
  ),0);
  -- Platform Stars charges are real paid funds, not refund credits.
  -- Reversed or unapplied charges must not count toward paid_rub.
  received:=received+coalesce((
    SELECT sum(c.applied_rub)
    FROM public.platform_saas_stars_charges c
    LEFT JOIN public.platform_saas_stars_refunds r
      ON r.order_id=c.order_id AND r.charge_id=c.charge_id
    WHERE c.invoice_id=bill.id AND r.id IS NULL
  ),0);
  received:=received+coalesce((
    SELECT sum(c.applied_rub)
    FROM public.platform_saas_gateway_charges c
    WHERE c.invoice_id=bill.id
  ),0)-coalesce((
    SELECT sum(r.reversed_rub)
    FROM public.platform_saas_gateway_refunds r
    JOIN public.platform_saas_gateway_charges c ON c.id=r.charge_id
    WHERE c.invoice_id=bill.id
  ),0);
  RETURN jsonb_build_object(
    'bot_id',bot_id,'project_id',p_project,'invoice_id',bill.id,
    'due_at',bill.due_at,
    'upcoming_at',CASE WHEN remaining=0 THEN coalesce((
      SELECT a.effective_end
      FROM public.platform_saas_bot_service_activations a
      WHERE a.invoice_id=bill.id
    ),bill.period_end) ELSE NULL END,
    'hold_state',public.platform_saas_owner_overdue_state(
       actor,clock_timestamp()
    ),
    'review_until',CASE WHEN remaining>0 THEN (
      SELECT min(p.submitted_at)+interval '12 hours'
      FROM public.platform_saas_payment_proofs_v2 p
      JOIN public.platform_saas_payment_attempts_v2 a
        ON a.id=p.attempt_id
      WHERE a.invoice_id=bill.id
        AND NOT EXISTS (
          SELECT 1 FROM public.platform_saas_payment_rejections_v2 bad
          JOIN public.platform_saas_payment_attempts_v2 ra
            ON ra.id=bad.attempt_id
          WHERE ra.invoice_id=bill.id
        )
        AND NOT EXISTS (
          SELECT 1 FROM public.platform_saas_payment_allocations_v2 ok
          JOIN public.platform_saas_payment_attempts_v2 oa
            ON oa.id=ok.attempt_id
          WHERE oa.invoice_id=bill.id
        )
        AND EXISTS (
          SELECT 1 FROM public.platform_saas_payment_proofs_v2 pending
          JOIN public.platform_saas_payment_attempts_v2 aa
            ON aa.id=pending.attempt_id
          WHERE aa.invoice_id=bill.id AND
            NOT EXISTS (
              SELECT 1 FROM public.platform_saas_payment_allocations_v2 yes
              WHERE yes.attempt_id=aa.id
            ) AND NOT EXISTS (
              SELECT 1 FROM public.platform_saas_payment_rejections_v2 no
              WHERE no.attempt_id=aa.id
            )
        )
    ) ELSE NULL END,
    'period_start',coalesce((
      SELECT a.effective_start
      FROM public.platform_saas_bot_service_activations a
      WHERE a.invoice_id=bill.id
    ),bill.period_start),
    'period_end',coalesce((
      SELECT a.effective_end
      FROM public.platform_saas_bot_service_activations a
      WHERE a.invoice_id=bill.id
    ),bill.period_end),
    'turnover',turnover,'service_rub',service_rub::text,
    'service_native',service_native::text,
    'service_currency',service_currency,
    'issued_total_usd',CASE WHEN service_currency='USD'
      THEN bill_total_usd::text ELSE NULL END,
    'issued_due_usd',CASE WHEN service_currency='USD'
      THEN bill_due_usd::text ELSE NULL END,
    'subscription_usd',CASE WHEN service_currency='USD'
      THEN round(subscriptions_rub/service_fx,2)::text ELSE NULL END,
    'file_usd',CASE WHEN service_currency='USD'
      THEN round(files_rub/service_fx,2)::text ELSE NULL END,
    'subscription_rub',round(subscriptions_rub,2)::text,
    'file_rub',round(files_rub,2)::text,'total_rub',bill.total_rub::text,
    'paid_rub',received::text,
    'credit_rub',(bill.total_rub-remaining-received)::text,
    'total_due_rub',remaining::text,
    'first_period',NOT EXISTS(
      SELECT 1 FROM public.platform_saas_invoices prior
      WHERE prior.bot_instance_id=bot_id AND prior.owner_user_id=actor
        AND prior.kind='prepayment' AND prior.status='issued'
        AND coalesce((
          SELECT a.effective_end
          FROM public.platform_saas_bot_service_activations a
          WHERE a.invoice_id=prior.id
        ),prior.period_end)=bill.period_start
    )
  );
END; $f$
    """)

def downgrade() -> None:
    raise RuntimeError("Native provider refund financial evidence is irreversible")
