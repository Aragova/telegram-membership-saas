"""Platform-owned Telegram Stars receipts for immutable ViMi Pay invoices.

Revision ID: 0092_saas_platform_stars
Revises: 0091_saas_usd5

Operator-configured quote, central-bot-only charge evidence, exact RUB allocation,
idempotent provider charge IDs and refund evidence. Never alter historical
bank receipts or billed prices; this adds an independent payment rail.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0092_saas_platform_stars"
down_revision: str | None = "0091_saas_usd5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE public.platform_saas_stars_pricing (
    id integer PRIMARY KEY CHECK (id=1),
    rub_per_star numeric(38,18) NOT NULL
        CHECK (rub_per_star >= 0.01 AND rub_per_star <= 1000),
    updated_by uuid NOT NULL REFERENCES public.telegram_users(id),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
    """)
    op.execute("""
CREATE TABLE public.platform_saas_stars_orders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id uuid NOT NULL REFERENCES public.platform_saas_invoices(id),
    owner_user_id uuid NOT NULL REFERENCES public.telegram_users(id),
    payer_telegram_id bigint NOT NULL CHECK (payer_telegram_id>0),
    central_bot_id bigint NOT NULL CHECK (central_bot_id>0),
    invoice_payload text NOT NULL UNIQUE,
    rub_amount numeric(38,2) NOT NULL CHECK (rub_amount>0),
    stars_amount integer NOT NULL CHECK (stars_amount BETWEEN 1 AND 250000),
    rub_per_star numeric(38,18) NOT NULL CHECK (rub_per_star>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    prechecked_at timestamptz,
    CHECK (expires_at>created_at),
    CHECK (invoice_payload=('vps:'||id::text))
)
    """)
    op.execute("""
CREATE INDEX ix_saas_stars_orders_invoice
ON public.platform_saas_stars_orders(invoice_id,created_at DESC)
    """)
    op.execute("""
CREATE TABLE public.platform_saas_stars_charges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id uuid NOT NULL REFERENCES public.platform_saas_stars_orders(id),
    invoice_id uuid NOT NULL REFERENCES public.platform_saas_invoices(id),
    charge_id text NOT NULL UNIQUE CHECK (length(charge_id) BETWEEN 8 AND 255),
    stars_amount integer NOT NULL CHECK (stars_amount BETWEEN 1 AND 250000),
    applied_rub numeric(38,2) NOT NULL CHECK (applied_rub>=0),
    paid_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
    """)
    op.execute("""
CREATE INDEX ix_saas_stars_charge_invoice
ON public.platform_saas_stars_charges(invoice_id)
    """)
    op.execute("""
CREATE TABLE public.platform_saas_stars_refunds (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id uuid NOT NULL REFERENCES public.platform_saas_stars_orders(id),
    charge_id text NOT NULL UNIQUE CHECK (length(charge_id) BETWEEN 8 AND 255),
    refunded_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
    """)
    for table in (
        "platform_saas_stars_pricing",
        "platform_saas_stars_orders",
        "platform_saas_stars_charges",
        "platform_saas_stars_refunds",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
        op.execute(f"""
DO $r$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
    REVOKE ALL ON TABLE public.{table} FROM membership_runtime;
  END IF;
END $r$
        """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v2_remaining(p_invoice uuid)
RETURNS numeric LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE original numeric; received numeric; credited numeric;
        stars_credit numeric; verified_credit numeric; invoice_status text;
BEGIN
  SELECT total_rub,status INTO original,invoice_status
  FROM public.platform_saas_invoices WHERE id=p_invoice;
  IF original IS NULL THEN
    RAISE EXCEPTION 'Issued invoice required for balance'
      USING ERRCODE='42501';
  END IF;
  IF invoice_status<>'issued' THEN RETURN 0; END IF;
  SELECT coalesce(sum(amount_rub),0) INTO received
  FROM public.platform_saas_receipts WHERE invoice_id=p_invoice;
  SELECT coalesce(sum(allocated_rub),0) INTO credited
  FROM public.platform_saas_payment_allocations_v2 WHERE invoice_id=p_invoice;
  SELECT coalesce(sum(
      CASE WHEN r.id IS NULL THEN c.applied_rub ELSE 0 END
  ),0) INTO stars_credit
  FROM public.platform_saas_stars_charges c
  LEFT JOIN public.platform_saas_stars_refunds r
    ON r.order_id=c.order_id AND r.charge_id=c.charge_id
  WHERE c.invoice_id=p_invoice;
  SELECT coalesce(sum(amount_rub),0) INTO verified_credit
  FROM public.platform_saas_verified_credit_allocations
  WHERE invoice_id=p_invoice;
  IF received+credited+stars_credit+verified_credit>original THEN
    RAISE EXCEPTION 'Invoice overpayment violates accounting invariant'
      USING ERRCODE='23514';
  END IF;
  RETURN original-received-credited-stars_credit-verified_credit;
END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v2_invoice_balance(
   p_invoice uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; operator_access boolean; i record;
        legacy_paid numeric; allocated numeric; stars_credit numeric; verified_credit numeric;
        remaining numeric;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  SELECT EXISTS(SELECT 1 FROM public.telegram_users u
    JOIN public.platform_operators o ON o.telegram_user_id=u.telegram_user_id
    WHERE u.id=actor AND o.role='owner') INTO operator_access;
  SELECT * INTO i FROM public.platform_saas_invoices WHERE id=p_invoice;
  IF i.id IS NULL OR i.status<>'issued'
     OR (i.owner_user_id IS DISTINCT FROM actor AND NOT operator_access) THEN
    RAISE EXCEPTION 'Invoice unavailable' USING ERRCODE='42501';
  END IF;
  SELECT coalesce(sum(amount_rub),0) INTO legacy_paid
    FROM public.platform_saas_receipts WHERE invoice_id=i.id;
  SELECT coalesce(sum(allocated_rub),0) INTO allocated
    FROM public.platform_saas_payment_allocations_v2 WHERE invoice_id=i.id;
  SELECT coalesce(sum(
    CASE WHEN r.id IS NULL THEN c.applied_rub ELSE 0 END
  ),0) INTO stars_credit
  FROM public.platform_saas_stars_charges c
  LEFT JOIN public.platform_saas_stars_refunds r
    ON r.order_id=c.order_id AND r.charge_id=c.charge_id
  WHERE c.invoice_id=i.id;
  SELECT coalesce(sum(amount_rub),0) INTO verified_credit
  FROM public.platform_saas_verified_credit_allocations
  WHERE invoice_id=i.id;
  remaining:=i.total_rub-legacy_paid-allocated-stars_credit-verified_credit;
  IF remaining<0 THEN
    RAISE EXCEPTION 'Overpaid bill: accounting inconsistent'
      USING ERRCODE='23514';
  END IF;
  RETURN jsonb_build_object(
    'invoice_id',i.id,'owner_user_id',i.owner_user_id,
    'total_rub',i.total_rub::text,
    'settled_rub',(legacy_paid+allocated+stars_credit)::text,
    'remaining_rub',remaining::text,'paid',remaining=0,
    'partial_rub',allocated::text,'legacy_rub',legacy_paid::text,
    'stars_rub',stars_credit::text,
    'verified_refund_credit_rub',verified_credit::text
  );
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_set_rate(p_rub_per_star numeric)
RETURNS numeric LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid;
BEGIN
  actor:=public.platform_saas_require_owner();
  IF p_rub_per_star IS NULL OR p_rub_per_star<0.01
     OR p_rub_per_star>1000 OR
     p_rub_per_star<>round(p_rub_per_star,8) THEN
    RAISE EXCEPTION 'Invalid Stars payout quote' USING ERRCODE='22023';
  END IF;
  INSERT INTO public.platform_saas_stars_pricing
    (id,rub_per_star,updated_by)
  VALUES (1,p_rub_per_star,actor)
  ON CONFLICT(id) DO UPDATE SET
    rub_per_star=excluded.rub_per_star,
    updated_by=excluded.updated_by, updated_at=clock_timestamp();
  RETURN p_rub_per_star;
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_rate()
RETURNS numeric LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
BEGIN
  PERFORM public.platform_saas_require_owner();
  RETURN (SELECT rub_per_star FROM public.platform_saas_stars_pricing WHERE id=1);
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_create_order(
  p_invoice uuid,p_telegram_id bigint,p_central_bot bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; inv record; rate numeric; due numeric;
        qty numeric; created uuid; payload text; expiry timestamptz;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  IF actor IS NULL OR p_telegram_id IS NULL OR p_telegram_id<=0
     OR p_central_bot IS NULL OR p_central_bot<=0 OR
     NOT EXISTS(SELECT 1 FROM public.telegram_users u
       WHERE u.id=actor AND u.telegram_user_id=p_telegram_id) THEN
    RAISE EXCEPTION 'Stars payer identity mismatch' USING ERRCODE='42501';
  END IF;
  SELECT * INTO inv FROM public.platform_saas_invoices
    WHERE id=p_invoice FOR UPDATE;
  IF inv.id IS NULL OR inv.status<>'issued'
     OR inv.owner_user_id IS DISTINCT FROM actor THEN
    RAISE EXCEPTION 'Invoice no longer payable' USING ERRCODE='42501';
  END IF;
  IF inv.bot_instance_id IS NOT NULL
     AND NOT EXISTS(
       SELECT 1 FROM public.bot_instances b
       JOIN public.projects p ON p.id=b.project_id
       WHERE b.id=inv.bot_instance_id AND p.owner_user_id=actor
     ) THEN
    RAISE EXCEPTION 'Transferred invoice requires reconciliation'
      USING ERRCODE='42501';
  END IF;
  rate:=(SELECT rub_per_star FROM public.platform_saas_stars_pricing WHERE id=1);
  IF rate IS NULL THEN
    RAISE EXCEPTION 'Operator Stars price is not configured'
      USING ERRCODE='P6713';
  END IF;
  due:=public.platform_saas_v2_remaining(inv.id);
  IF due<=0 THEN
    RAISE EXCEPTION 'Invoice already settled' USING ERRCODE='23514';
  END IF;
  qty:=ceil(due/rate);
  IF qty<1 OR qty>250000 THEN
    RAISE EXCEPTION 'Invoice outside Telegram Stars checkout range'
      USING ERRCODE='22023';
  END IF;
  created:=gen_random_uuid();
  payload:='vps:'||created::text;
  expiry:=clock_timestamp()+interval '20 minutes';
  INSERT INTO public.platform_saas_stars_orders (
    id,invoice_id,owner_user_id,payer_telegram_id,central_bot_id,
    invoice_payload,rub_amount,stars_amount,rub_per_star,expires_at
  ) VALUES (
    created,p_invoice,actor,p_telegram_id,p_central_bot,
    payload,due,qty::integer,rate,expiry
  );
  RETURN jsonb_build_object(
    'order_id',created,'payload',payload,'stars_amount',qty::integer,
    'rub_amount',due::text,'expires_at',expiry
  );
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_precheckout(
  p_payload text,p_telegram_id bigint,p_bot_id bigint,p_amount integer
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; ord record; inv record; moment timestamptz;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  IF actor IS NULL OR p_payload IS NULL OR
     p_telegram_id IS NULL OR p_bot_id IS NULL OR
     p_amount IS NULL OR p_amount<1 THEN RETURN false; END IF;
  SELECT * INTO ord FROM public.platform_saas_stars_orders
    WHERE invoice_payload=p_payload FOR UPDATE;
  IF ord.id IS NULL OR ord.owner_user_id IS DISTINCT FROM actor
     OR ord.payer_telegram_id<>p_telegram_id
     OR ord.central_bot_id<>p_bot_id OR ord.stars_amount<>p_amount
     OR NOT EXISTS(SELECT 1 FROM public.telegram_users u
       WHERE u.id=actor AND u.telegram_user_id=p_telegram_id) THEN
    RETURN false;
  END IF;
  SELECT * INTO inv FROM public.platform_saas_invoices
    WHERE id=ord.invoice_id FOR UPDATE;
  moment:=clock_timestamp();
  IF inv.id IS NULL OR inv.status<>'issued'
     OR inv.owner_user_id IS DISTINCT FROM actor
     OR moment>ord.expires_at
     OR ord.prechecked_at IS NOT NULL
     OR EXISTS(SELECT 1 FROM public.platform_saas_stars_charges c
       WHERE c.order_id=ord.id)
     OR EXISTS(SELECT 1 FROM public.platform_saas_stars_refunds r
       WHERE r.order_id=ord.id)
     OR public.platform_saas_v2_remaining(inv.id)<>ord.rub_amount
     OR (inv.bot_instance_id IS NOT NULL AND NOT EXISTS(
       SELECT 1 FROM public.bot_instances b
       JOIN public.projects p ON p.id=b.project_id
       WHERE b.id=inv.bot_instance_id AND p.owner_user_id=actor
     ))
     OR EXISTS(SELECT 1 FROM public.platform_saas_stars_orders other
       WHERE other.invoice_id=inv.id AND other.id<>ord.id
         AND other.prechecked_at>moment-interval '24 hours'
         AND NOT EXISTS(SELECT 1 FROM public.platform_saas_stars_charges c
           WHERE c.order_id=other.id))
  THEN RETURN false; END IF;
  UPDATE public.platform_saas_stars_orders SET prechecked_at=moment
  WHERE id=ord.id;
  RETURN true;
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_record_charge(
  p_payload text,p_telegram_id bigint,p_bot_id bigint,p_amount integer,
  p_charge_id text,p_paid_at timestamptz
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; ord record; inv record; old record;
        ownership_owner uuid; rub_allocated numeric:=0;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  IF actor IS NULL OR p_payload IS NULL OR p_paid_at IS NULL OR
     p_charge_id IS NULL OR length(p_charge_id) NOT BETWEEN 8 AND 255 OR
     p_telegram_id IS NULL OR p_bot_id IS NULL OR p_amount IS NULL THEN
    RAISE EXCEPTION 'Invalid Stars charge evidence' USING ERRCODE='22023';
  END IF;
  SELECT * INTO ord FROM public.platform_saas_stars_orders
    WHERE invoice_payload=p_payload FOR UPDATE;
  IF ord.id IS NULL OR ord.owner_user_id IS DISTINCT FROM actor
     OR ord.payer_telegram_id<>p_telegram_id
     OR ord.central_bot_id<>p_bot_id OR ord.stars_amount<>p_amount
     OR NOT EXISTS(SELECT 1 FROM public.telegram_users u
       WHERE u.id=actor AND u.telegram_user_id=p_telegram_id)
     OR p_paid_at<ord.created_at-interval '5 minutes'
     OR p_paid_at>clock_timestamp()+interval '1 minute'
  THEN
    RAISE EXCEPTION 'Stars receipt does not match its invoice'
      USING ERRCODE='42501';
  END IF;
  SELECT * INTO old FROM public.platform_saas_stars_charges
    WHERE charge_id=p_charge_id FOR UPDATE;
  IF old.id IS NOT NULL THEN
    IF old.order_id<>ord.id OR old.charge_id<>p_charge_id
       OR old.stars_amount<>p_amount THEN
      RAISE EXCEPTION 'Conflicting Telegram Stars charge replay'
        USING ERRCODE='23505';
    END IF;
    RETURN jsonb_build_object(
      'applied',old.applied_rub>0,'allocated_rub',old.applied_rub::text,
      'duplicate',true,'requires_review',old.applied_rub=0
    );
  END IF;
  SELECT * INTO inv FROM public.platform_saas_invoices
    WHERE id=ord.invoice_id FOR UPDATE;
  IF inv.id IS NULL THEN
    RAISE EXCEPTION 'Missing original Stars invoice'
      USING ERRCODE='42501';
  END IF;
  -- Pin actual project ownership during allocation and activation.
  -- A racing bot transfer must not roll back an already debited charge.
  IF inv.bot_instance_id IS NOT NULL THEN
    SELECT p.owner_user_id INTO ownership_owner
    FROM public.bot_instances b
    JOIN public.projects p ON p.id=b.project_id
    WHERE b.id=inv.bot_instance_id
    FOR SHARE OF p;
  END IF;
  -- A genuinely charged Telegram update is always journaled.
  -- Never allocate twice after a concurrent bank payment or transfer.
  IF inv.status='issued' AND inv.owner_user_id=actor
     AND ord.prechecked_at IS NOT NULL
     AND NOT EXISTS(SELECT 1 FROM public.platform_saas_stars_charges c
       WHERE c.order_id=ord.id)
     AND NOT EXISTS(SELECT 1 FROM public.platform_saas_stars_refunds r
       WHERE r.order_id=ord.id)
     AND public.platform_saas_v2_remaining(inv.id)=ord.rub_amount
     AND (inv.bot_instance_id IS NULL OR ownership_owner=actor)
  THEN rub_allocated:=ord.rub_amount; END IF;
  INSERT INTO public.platform_saas_stars_charges
    (order_id,invoice_id,charge_id,stars_amount,applied_rub,paid_at)
  VALUES (ord.id,inv.id,p_charge_id,p_amount,rub_allocated,p_paid_at);
  RETURN jsonb_build_object(
    'applied',rub_allocated>0,'allocated_rub',rub_allocated::text,
    'duplicate',false,'requires_review',rub_allocated=0
  );
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_record_refund(
  p_payload text,p_telegram_id bigint,p_bot_id bigint,p_amount integer,
  p_charge_id text,p_refunded_at timestamptz
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; ord record; old record;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  IF actor IS NULL OR p_payload IS NULL OR p_charge_id IS NULL
     OR length(p_charge_id) NOT BETWEEN 8 AND 255 OR p_refunded_at IS NULL
     OR p_refunded_at>clock_timestamp()+interval '1 minute' THEN
    RAISE EXCEPTION 'Invalid Stars refund evidence' USING ERRCODE='22023';
  END IF;
  SELECT * INTO ord FROM public.platform_saas_stars_orders
    WHERE invoice_payload=p_payload FOR UPDATE;
  IF ord.id IS NULL OR ord.owner_user_id IS DISTINCT FROM actor
     OR ord.payer_telegram_id<>p_telegram_id
     OR ord.central_bot_id<>p_bot_id OR ord.stars_amount<>p_amount THEN
    RAISE EXCEPTION 'Stars refund identity mismatch' USING ERRCODE='42501';
  END IF;
  SELECT * INTO old FROM public.platform_saas_stars_refunds
    WHERE charge_id=p_charge_id FOR UPDATE;
  IF old.id IS NOT NULL THEN
    IF old.order_id<>ord.id OR old.charge_id<>p_charge_id THEN
      RAISE EXCEPTION 'Conflicting Stars refund replay' USING ERRCODE='23505';
    END IF;
    RETURN false;
  END IF;
  IF EXISTS(SELECT 1 FROM public.platform_saas_stars_charges c
     WHERE c.charge_id=p_charge_id AND c.order_id<>ord.id) THEN
    RAISE EXCEPTION 'Stars refund charge mismatch' USING ERRCODE='23505';
  END IF;
  INSERT INTO public.platform_saas_stars_refunds
    (order_id,charge_id,refunded_at)
  VALUES (ord.id,p_charge_id,p_refunded_at);
  RETURN true;
END; $f$
    """)
    # Preserve the existing operator/merchant bill JSON shape and verified
    # refund-credit classification when adding Stars paid funds.
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
  SELECT l.original_amount,l.original_currency
  INTO service_native,service_currency
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
    # Match the existing owner-scoped, SECURITY DEFINER merchant bill RPC.
    # CI creates membership_runtime after migrations; its grant is mirrored
    # in the GitHub Actions role bootstrap.
    op.execute("""
DO $grant$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
  THEN
    GRANT EXECUTE ON FUNCTION
      public.platform_saas_merchant_bill_summary(date,uuid)
    TO membership_runtime;
  END IF;
END $grant$
    """)
    # Prevent a bank confirmation from racing a paid Stars checkout.
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_cap_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE remaining numeric; bill uuid;
BEGIN
  bill:=NEW.invoice_id;
  PERFORM 1 FROM public.platform_saas_invoices i WHERE i.id=bill FOR UPDATE;
  remaining:=public.platform_saas_v2_remaining(bill);
  IF remaining<NEW.amount_rub THEN
    RAISE EXCEPTION 'Payment exceeds outstanding invoice including Stars'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END; $f$
    """)
    op.execute("""
CREATE TRIGGER trg_saas_receipt_stars_cap
BEFORE INSERT ON public.platform_saas_receipts
FOR EACH ROW EXECUTE FUNCTION public.platform_saas_stars_cap_guard()
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_bank_allocation_cap_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
BEGIN
  PERFORM 1 FROM public.platform_saas_invoices i
  WHERE i.id=NEW.invoice_id FOR UPDATE;
  IF public.platform_saas_v2_remaining(NEW.invoice_id)<NEW.allocated_rub THEN
    RAISE EXCEPTION 'Bank allocation exceeds remaining invoice including Stars'
      USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END; $f$
    """)
    op.execute("""
CREATE TRIGGER trg_saas_bank_allocation_stars_cap
BEFORE INSERT ON public.platform_saas_payment_allocations_v2
FOR EACH ROW EXECUTE FUNCTION
  public.platform_saas_stars_bank_allocation_cap_guard()
    """)
    op.execute("""
CREATE TRIGGER trg_saas_activation_stars_charge
AFTER INSERT ON public.platform_saas_stars_charges
FOR EACH ROW WHEN (NEW.applied_rub > 0)
EXECUTE FUNCTION public.platform_saas_record_bot_service_activation()
    """)
    # Append-only financial evidence; only a narrow precheckout status can
    # change, and that write is restricted to the SECURITY DEFINER RPC above.
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_append_only()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
BEGIN
  RAISE EXCEPTION 'Verified Stars evidence is immutable' USING ERRCODE='42501';
END; $f$
    """)
    for table in ("platform_saas_stars_charges","platform_saas_stars_refunds"):
        op.execute(f"""
CREATE TRIGGER trg_{table}_immutable
BEFORE UPDATE OR DELETE ON public.{table}
FOR EACH ROW EXECUTE FUNCTION public.platform_saas_stars_append_only()
        """)
    for sig in (
        "platform_saas_stars_set_rate(numeric)",
        "platform_saas_stars_rate()",
        "platform_saas_stars_create_order(uuid,bigint,bigint)",
        "platform_saas_stars_precheckout(text,bigint,bigint,integer)",
        "platform_saas_stars_record_charge(text,bigint,bigint,integer,text,timestamptz)",
        "platform_saas_stars_record_refund(text,bigint,bigint,integer,text,timestamptz)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{sig} FROM PUBLIC")
        op.execute(f"""
DO $r$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
    GRANT EXECUTE ON FUNCTION public.{sig} TO membership_runtime;
  END IF;
END $r$
        """)
    for sig in (
        "platform_saas_stars_cap_guard()",
        "platform_saas_stars_bank_allocation_cap_guard()",
        "platform_saas_stars_append_only()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{sig} FROM PUBLIC")


def downgrade() -> None:
    raise RuntimeError("Refuse to discard verified Telegram Stars financial evidence")
