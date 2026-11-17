"""Isolated platform external-provider orders and verified settlement ledger.

Revision ID: 0093_saas_external_gateways
Revises: 0092_saas_platform_stars

All existing 150 RUB / historical USD source prices remain unchanged.
No provider is enabled by a migration. Only authenticated platform-owner
worker RPCs can record provider-confirmed cash. All duplicate, late or
misattributed paid events are journaled without a second service activation.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0093_saas_external_gateways"
down_revision: str | None = "0092_saas_platform_stars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE public.platform_saas_gateway_orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES public.platform_saas_invoices(id),
  owner_user_id uuid NOT NULL REFERENCES public.telegram_users(id),
  payer_telegram_id bigint NOT NULL CHECK (payer_telegram_id>0),
  central_bot_id bigint NOT NULL CHECK (central_bot_id>0),
  provider text NOT NULL CHECK (provider IN ('crypto','lava')),
  invoice_payload text NOT NULL UNIQUE,
  amount_rub numeric(38,2) NOT NULL CHECK (amount_rub>0),
  status text NOT NULL DEFAULT 'reserved'
    CHECK (status IN ('reserved','issued')),
  provider_invoice_id text,
  provider_invoice_url text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  CHECK (expires_at>created_at),
  CHECK (invoice_payload=(CASE provider
    WHEN 'crypto' THEN 'vpc:' ELSE 'vlv:' END)||id::text),
  CHECK ((status='reserved' AND provider_invoice_id IS NULL
          AND provider_invoice_url IS NULL) OR
         (status='issued' AND provider_invoice_id IS NOT NULL
          AND provider_invoice_url IS NOT NULL)),
  UNIQUE (provider,provider_invoice_id)
)
    """)
    op.execute("""
CREATE INDEX ix_saas_gateway_invoice
ON public.platform_saas_gateway_orders(invoice_id,created_at DESC)
    """)
    op.execute("""
CREATE TABLE public.platform_saas_gateway_charges (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id uuid NOT NULL UNIQUE REFERENCES public.platform_saas_gateway_orders(id),
  invoice_id uuid NOT NULL REFERENCES public.platform_saas_invoices(id),
  provider text NOT NULL CHECK (provider IN ('crypto','lava')),
  provider_invoice_id text NOT NULL,
  amount_rub numeric(38,2) NOT NULL CHECK (amount_rub>0),
  applied_rub numeric(38,2) NOT NULL CHECK (applied_rub>=0),
  paid_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(provider,provider_invoice_id),
  CHECK (applied_rub<=amount_rub)
)
    """)
    op.execute("""
CREATE TABLE public.platform_saas_gateway_refunds (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  charge_id uuid NOT NULL REFERENCES public.platform_saas_gateway_charges(id),
  provider text NOT NULL CHECK (provider IN ('crypto','lava')),
  provider_refund_id text NOT NULL,
  reversed_rub numeric(38,2) NOT NULL CHECK (reversed_rub>0),
  refunded_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(provider,provider_refund_id)
)
    """)
    for table in (
        "platform_saas_gateway_orders",
        "platform_saas_gateway_charges",
        "platform_saas_gateway_refunds",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
        op.execute(f"""
DO $r$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
   REVOKE ALL ON TABLE public.{table} FROM membership_runtime;
 END IF;
END $r$
        """)

    # Every legacy balance/hold/report uses these authoritative replacements
    # rather than silently calling a new separate provider billing balance.
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_v2_remaining(p_invoice uuid)
RETURNS numeric LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE original numeric; received numeric; credited numeric;
        stars_credit numeric; verified_credit numeric; invoice_status text;
        gateway_paid numeric; gateway_refunded numeric;
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
  SELECT coalesce(sum(c.applied_rub),0),
         coalesce((SELECT sum(r.reversed_rub)
           FROM public.platform_saas_gateway_refunds r
           JOIN public.platform_saas_gateway_charges x ON x.id=r.charge_id
           WHERE x.invoice_id=p_invoice),0)
    INTO gateway_paid,gateway_refunded
  FROM public.platform_saas_gateway_charges c
  WHERE c.invoice_id=p_invoice;
  SELECT coalesce(sum(amount_rub),0) INTO verified_credit
  FROM public.platform_saas_verified_credit_allocations
  WHERE invoice_id=p_invoice;
  IF received+credited+stars_credit+verified_credit+gateway_paid-gateway_refunded>original THEN
    RAISE EXCEPTION 'Invoice overpayment violates accounting invariant'
      USING ERRCODE='23514';
  END IF;
  RETURN original-received-credited-stars_credit-verified_credit-gateway_paid+gateway_refunded;
END; $f$""")
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_v2_invoice_balance(
   p_invoice uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; operator_access boolean; i record;
        legacy_paid numeric; allocated numeric; stars_credit numeric; verified_credit numeric;
        remaining numeric; gateway_paid numeric; gateway_refunded numeric;
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
  SELECT coalesce(sum(c.applied_rub),0),
         coalesce((SELECT sum(r.reversed_rub)
           FROM public.platform_saas_gateway_refunds r
           JOIN public.platform_saas_gateway_charges x ON x.id=r.charge_id
           WHERE x.invoice_id=i.id),0)
    INTO gateway_paid,gateway_refunded
  FROM public.platform_saas_gateway_charges c
  WHERE c.invoice_id=i.id;
  SELECT coalesce(sum(amount_rub),0) INTO verified_credit
  FROM public.platform_saas_verified_credit_allocations
  WHERE invoice_id=i.id;
  remaining:=i.total_rub-legacy_paid-allocated-stars_credit-verified_credit-gateway_paid+gateway_refunded;
  IF remaining<0 THEN
    RAISE EXCEPTION 'Overpaid bill: accounting inconsistent'
      USING ERRCODE='23514';
  END IF;
  RETURN jsonb_build_object(
    'invoice_id',i.id,'owner_user_id',i.owner_user_id,
    'total_rub',i.total_rub::text,
    'settled_rub',(legacy_paid+allocated+stars_credit+gateway_paid-gateway_refunded)::text,
    'remaining_rub',remaining::text,'paid',remaining=0,
    'partial_rub',allocated::text,'legacy_rub',legacy_paid::text,
    'stars_rub',stars_credit::text,
    'gateway_rub',(gateway_paid-gateway_refunded)::text,
    'verified_refund_credit_rub',verified_credit::text
  );
END; $f$""")
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_merchant_bill_summary(
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
END; $f$""")
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_invoice_report(
            p_page integer, p_size integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE result jsonb;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_page IS NULL OR p_page<0 OR p_page>100000
             OR p_size IS NULL OR p_size<1 OR p_size>10 THEN
            RAISE EXCEPTION 'Invalid invoice page' USING ERRCODE='22023';
          END IF;
          SELECT jsonb_build_object(
              'enabled', (SELECT enabled FROM public.platform_saas_billing_control
                          WHERE id=1),
              'total', (SELECT count(*) FROM public.platform_saas_invoices
                        WHERE status='issued'),
              'unpaid', (SELECT count(*) FROM public.platform_saas_invoices i
                         WHERE i.status='issued' AND public.platform_saas_v2_remaining(i.id)>0),
              'unpaid_rub', (SELECT coalesce(sum(public.platform_saas_v2_remaining(i.id)),0)::text
                             FROM public.platform_saas_invoices i
                             WHERE i.status='issued' AND public.platform_saas_v2_remaining(i.id)>0),
              'due_now_count',(SELECT count(*)
                FROM public.platform_saas_invoices i
                WHERE i.status='issued' AND i.due_at<=clock_timestamp()
                  AND public.platform_saas_v2_remaining(i.id)>0),
              'due_now_rub',(SELECT coalesce(
                sum(public.platform_saas_v2_remaining(i.id)),0)::text
                FROM public.platform_saas_invoices i
                WHERE i.status='issued' AND i.due_at<=clock_timestamp()
                  AND public.platform_saas_v2_remaining(i.id)>0),
              'upcoming_count',(SELECT count(*)
                FROM public.platform_saas_invoices i
                WHERE i.status='issued' AND i.due_at>clock_timestamp()
                  AND public.platform_saas_v2_remaining(i.id)>0),
              'upcoming_rub',(SELECT coalesce(
                sum(public.platform_saas_v2_remaining(i.id)),0)::text
                FROM public.platform_saas_invoices i
                WHERE i.status='issued' AND i.due_at>clock_timestamp()
                  AND public.platform_saas_v2_remaining(i.id)>0),
              'received_rub', (
                (SELECT coalesce(sum(r.amount_rub),0)
                 FROM public.platform_saas_receipts r)
                +(SELECT coalesce(sum(a.allocated_rub),0)
                  FROM public.platform_saas_payment_allocations_v2 a)
                +(SELECT coalesce(sum(c.applied_rub),0)
                  FROM public.platform_saas_stars_charges c
                  LEFT JOIN public.platform_saas_stars_refunds r
                    ON r.order_id=c.order_id AND r.charge_id=c.charge_id
                  WHERE r.id IS NULL)
                +(SELECT coalesce(sum(c.applied_rub),0)
                  FROM public.platform_saas_gateway_charges c)
                -(SELECT coalesce(sum(r.reversed_rub),0)
                  FROM public.platform_saas_gateway_refunds r)
                )::text,
              'pages', (SELECT CASE WHEN count(*)=0 THEN 1
                               ELSE ceil(count(*)::numeric/p_size)::int END
                        FROM public.platform_saas_invoices
                        WHERE status='issued'),
              'invoices', coalesce((
                  SELECT jsonb_agg(jsonb_build_object(
                    'id',j.id::text, 'owner',j.owner_user_id::text,
                    'merchant',j.merchant_id::text,
                    'kind',j.kind, 'total_rub',j.total_rub::text,
                    'remaining_rub',public.platform_saas_v2_remaining(j.id)::text,
                    'invoice_key',j.invoice_key,
                    'bot',j.bot_instance_id::text,
                    'period_start',j.period_start,'period_end',j.period_end,
                    'due_at',j.due_at, 'status',
                    CASE WHEN public.platform_saas_v2_remaining(j.id)=0 THEN 'paid'
                         ELSE j.status END
                  ) ORDER BY j.due_at DESC,j.id DESC)
                  FROM (
                    SELECT i.*
                    FROM public.platform_saas_invoices i
                    WHERE i.status='issued'
                    ORDER BY i.due_at DESC,i.id DESC
                    LIMIT p_size OFFSET p_page*p_size
                  ) j
              ),'[]'::jsonb)
          ) INTO result;
          RETURN result;
        END;
        $f$""")

    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_reserve(
  p_invoice uuid,p_provider text,p_telegram_id bigint,p_bot_id bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; i record; prior record; due numeric;
        created uuid; payload text; expiry timestamptz;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  IF actor IS NULL OR p_invoice IS NULL
     OR p_provider NOT IN ('crypto','lava')
     OR p_telegram_id IS NULL OR p_telegram_id<=0
     OR p_bot_id IS NULL OR p_bot_id<=0
     OR NOT EXISTS(SELECT 1 FROM public.telegram_users u
       WHERE u.id=actor AND u.telegram_user_id=p_telegram_id) THEN
    RAISE EXCEPTION 'Invalid platform gateway identity'
      USING ERRCODE='42501';
  END IF;
  SELECT * INTO i FROM public.platform_saas_invoices
  WHERE id=p_invoice FOR UPDATE;
  IF i.id IS NULL OR i.status<>'issued'
     OR i.owner_user_id IS DISTINCT FROM actor
     OR (i.bot_instance_id IS NOT NULL AND NOT EXISTS(
       SELECT 1 FROM public.bot_instances b
       JOIN public.projects p ON p.id=b.project_id
       WHERE b.id=i.bot_instance_id AND p.owner_user_id=actor
     )) THEN
    RAISE EXCEPTION 'Invoice no longer payable by this owner'
      USING ERRCODE='42501';
  END IF;
  due:=public.platform_saas_v2_remaining(i.id);
  IF due<=0 OR (p_provider='lava' AND due<50) THEN
    RAISE EXCEPTION 'Outstanding balance is not payable by gateway'
      USING ERRCODE='22023';
  END IF;
  SELECT * INTO prior FROM public.platform_saas_gateway_orders o
  WHERE o.invoice_id=i.id AND o.provider=p_provider
    AND o.owner_user_id=actor AND o.expires_at>clock_timestamp()
  ORDER BY o.created_at DESC FOR UPDATE LIMIT 1;
  IF prior.id IS NOT NULL THEN
    IF prior.amount_rub<>due THEN
      RAISE EXCEPTION 'Older gateway checkout must expire before new quote'
        USING ERRCODE='P6723';
    END IF;
    RETURN jsonb_build_object(
      'id',prior.id,'payload',prior.invoice_payload,
      'status',prior.status,'amount_rub',prior.amount_rub::text,
      'provider_invoice_id',prior.provider_invoice_id,
      'url',prior.provider_invoice_url,'expires_at',prior.expires_at
    );
  END IF;
  created:=gen_random_uuid();
  payload:=(CASE p_provider WHEN 'crypto' THEN 'vpc:'
    ELSE 'vlv:' END)||created::text;
  expiry:=clock_timestamp()+interval '30 minutes';
  INSERT INTO public.platform_saas_gateway_orders(
    id,invoice_id,owner_user_id,payer_telegram_id,central_bot_id,
    provider,invoice_payload,amount_rub,expires_at
  ) VALUES(
    created,i.id,actor,p_telegram_id,p_bot_id,p_provider,
    payload,due,expiry
  );
  RETURN jsonb_build_object(
    'id',created,'payload',payload,'status','reserved',
    'amount_rub',due::text,'provider_invoice_id',NULL,
    'url',NULL,'expires_at',expiry
  );
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_bind(
  p_order uuid,p_telegram_id bigint,p_bot_id bigint,
  p_provider_invoice_id text,p_provider_url text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; o record;
BEGIN
  -- A merchant may reserve their own bill but only the platform's
  -- authenticated reconciliation service can bind a provider payment URL.
  actor:=public.platform_saas_require_owner();
  IF p_order IS NULL OR p_provider_invoice_id IS NULL
     OR length(p_provider_invoice_id) NOT BETWEEN 1 AND 128
     OR p_provider_url IS NULL OR length(p_provider_url)>2048 THEN
    RAISE EXCEPTION 'Invalid gateway invoice reference'
      USING ERRCODE='22023';
  END IF;
  SELECT * INTO o FROM public.platform_saas_gateway_orders
  WHERE id=p_order FOR UPDATE;
  IF o.id IS NULL
    OR o.payer_telegram_id<>p_telegram_id
    OR o.central_bot_id<>p_bot_id OR o.expires_at<clock_timestamp() THEN
    RAISE EXCEPTION 'Provider checkout owner mismatch'
      USING ERRCODE='42501';
  END IF;
  IF o.status='issued' THEN
    RETURN o.provider_invoice_id=p_provider_invoice_id
       AND o.provider_invoice_url=p_provider_url;
  END IF;
  -- The exact HTTPS link and signed provider identity are independently
  -- verified by the platform-only service before this binding call.
  UPDATE public.platform_saas_gateway_orders
  SET status='issued',provider_invoice_id=p_provider_invoice_id,
      provider_invoice_url=p_provider_url
  WHERE id=o.id;
  RETURN true;
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_pending(p_limit integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
BEGIN
  PERFORM public.platform_saas_require_owner();
  IF p_limit IS NULL OR p_limit<1 OR p_limit>100 THEN
    RAISE EXCEPTION 'Invalid gateway page size' USING ERRCODE='22023';
  END IF;
  RETURN coalesce((
    SELECT jsonb_agg(to_jsonb(q)) FROM (
      SELECT o.id,o.invoice_id,o.owner_user_id,
         o.payer_telegram_id,o.central_bot_id,o.provider,
         o.invoice_payload,o.amount_rub::text AS amount_rub,
         o.provider_invoice_id,o.created_at
      FROM public.platform_saas_gateway_orders o
      WHERE o.status='issued'
        AND NOT EXISTS(SELECT 1 FROM public.platform_saas_gateway_charges c
                       WHERE c.order_id=o.id)
      ORDER BY o.created_at ASC LIMIT p_limit
    ) q
  ),'[]'::jsonb);
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_record_charge(
  p_order uuid,p_provider text,p_provider_id text,
  p_amount_rub numeric,p_paid_at timestamptz
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE o record; i record; old record;
        ownership_owner uuid; allocated numeric:=0;
BEGIN
  PERFORM public.platform_saas_require_owner();
  IF p_order IS NULL OR p_provider NOT IN ('crypto','lava')
     OR p_provider_id IS NULL OR p_paid_at IS NULL
     OR p_amount_rub IS NULL OR p_amount_rub<=0
     OR p_amount_rub<>round(p_amount_rub,2)
     OR p_paid_at>clock_timestamp()+interval '5 minutes' THEN
    RAISE EXCEPTION 'Invalid authoritative gateway receipt'
      USING ERRCODE='22023';
  END IF;
  SELECT * INTO o FROM public.platform_saas_gateway_orders
    WHERE id=p_order FOR UPDATE;
  IF o.id IS NULL OR o.status<>'issued'
     OR o.provider<>p_provider
     OR o.provider_invoice_id IS DISTINCT FROM p_provider_id
     OR o.amount_rub<>p_amount_rub THEN
    RAISE EXCEPTION 'Provider invoice not bound to frozen SaaS order'
      USING ERRCODE='42501';
  END IF;
  SELECT * INTO old FROM public.platform_saas_gateway_charges
    WHERE provider=p_provider AND provider_invoice_id=p_provider_id FOR UPDATE;
  IF old.id IS NOT NULL THEN
    IF old.order_id<>o.id OR old.amount_rub<>p_amount_rub THEN
      RAISE EXCEPTION 'Conflicting platform provider receipt'
        USING ERRCODE='23505';
    END IF;
    RETURN jsonb_build_object(
      'duplicate',true,'applied',old.applied_rub>0,
      'applied_rub',old.applied_rub::text
    );
  END IF;
  SELECT * INTO i FROM public.platform_saas_invoices
  WHERE id=o.invoice_id FOR UPDATE;
  IF i.id IS NULL THEN
    RAISE EXCEPTION 'Missing original SaaS invoice' USING ERRCODE='42501';
  END IF;
  IF i.bot_instance_id IS NOT NULL THEN
    SELECT p.owner_user_id INTO ownership_owner
    FROM public.bot_instances b
    JOIN public.projects p ON p.id=b.project_id
    WHERE b.id=i.bot_instance_id FOR SHARE OF p;
  END IF;
  IF i.status='issued' AND i.owner_user_id=o.owner_user_id
     AND (i.bot_instance_id IS NULL OR ownership_owner=o.owner_user_id)
     AND public.platform_saas_v2_remaining(i.id)=o.amount_rub
  THEN allocated:=o.amount_rub; END IF;
  INSERT INTO public.platform_saas_gateway_charges(
    order_id,invoice_id,provider,provider_invoice_id,
    amount_rub,applied_rub,paid_at
  ) VALUES(o.id,i.id,p_provider,p_provider_id,p_amount_rub,allocated,p_paid_at);
  RETURN jsonb_build_object(
    'duplicate',false,'applied',allocated>0,
    'applied_rub',allocated::text,'requires_review',allocated=0
  );
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_record_refund(
  p_order uuid,p_provider text,p_provider_invoice_id text,
  p_refund_id text,p_reversed_rub numeric,p_refunded_at timestamptz
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE c record; old record; already numeric;
BEGIN
  PERFORM public.platform_saas_require_owner();
  IF p_order IS NULL OR p_provider NOT IN ('crypto','lava')
     OR p_provider_invoice_id IS NULL OR p_refund_id IS NULL
     OR length(p_refund_id) NOT BETWEEN 1 AND 128
     OR p_refunded_at IS NULL OR p_reversed_rub IS NULL
     OR p_reversed_rub<=0 OR p_reversed_rub<>round(p_reversed_rub,2)
  THEN
    RAISE EXCEPTION 'Invalid provider refund evidence' USING ERRCODE='22023';
  END IF;
  SELECT * INTO c FROM public.platform_saas_gateway_charges
  WHERE order_id=p_order AND provider=p_provider
    AND provider_invoice_id=p_provider_invoice_id FOR UPDATE;
  IF c.id IS NULL THEN
    RAISE EXCEPTION 'Original paid provider invoice required'
      USING ERRCODE='42501';
  END IF;
  SELECT * INTO old FROM public.platform_saas_gateway_refunds
    WHERE provider=p_provider AND provider_refund_id=p_refund_id;
  IF old.id IS NOT NULL THEN
    IF old.charge_id<>c.id OR old.reversed_rub<>p_reversed_rub THEN
      RAISE EXCEPTION 'Conflicting provider refund replay'
        USING ERRCODE='23505';
    END IF;
    RETURN false;
  END IF;
  SELECT coalesce(sum(reversed_rub),0) INTO already
  FROM public.platform_saas_gateway_refunds WHERE charge_id=c.id;
  IF p_reversed_rub+already>c.applied_rub THEN
    RAISE EXCEPTION 'Provider refund exceeds original credited payment'
      USING ERRCODE='23514';
  END IF;
  PERFORM 1 FROM public.platform_saas_invoices
    WHERE id=c.invoice_id FOR UPDATE;
  INSERT INTO public.platform_saas_gateway_refunds(
    charge_id,provider,provider_refund_id,reversed_rub,refunded_at
  ) VALUES(c.id,p_provider,p_refund_id,p_reversed_rub,p_refunded_at);
  RETURN true;
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_block_void()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
BEGIN
  IF TG_OP='UPDATE' AND NEW.status='void' AND OLD.status<>'void'
     AND EXISTS(SELECT 1 FROM public.platform_saas_gateway_charges c
                WHERE c.invoice_id=OLD.id) THEN
    RAISE EXCEPTION 'Cannot void invoice with provider financial evidence'
      USING ERRCODE='P6722';
  END IF;
  RETURN NEW;
END; $f$
    """)
    op.execute("""
CREATE TRIGGER trg_saas_gateway_no_paid_void
BEFORE UPDATE OF status ON public.platform_saas_invoices
FOR EACH ROW EXECUTE FUNCTION public.platform_saas_gateway_block_void()
    """)
    op.execute("""
CREATE TRIGGER trg_saas_gateway_activation
AFTER INSERT ON public.platform_saas_gateway_charges
FOR EACH ROW WHEN (NEW.applied_rub>0)
EXECUTE FUNCTION public.platform_saas_record_bot_service_activation()
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_append_only()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
BEGIN
  RAISE EXCEPTION 'Verified gateway evidence is immutable'
    USING ERRCODE='42501';
END; $f$
    """)
    for table in ("platform_saas_gateway_charges","platform_saas_gateway_refunds"):
        op.execute(f"""
CREATE TRIGGER trg_{table}_immutable
BEFORE UPDATE OR DELETE ON public.{table}
FOR EACH ROW EXECUTE FUNCTION public.platform_saas_gateway_append_only()
        """)
    for sig in (
        "platform_saas_gateway_reserve(uuid,text,bigint,bigint)",
        "platform_saas_gateway_bind(uuid,bigint,bigint,text,text)",
        "platform_saas_gateway_pending(integer)",
        "platform_saas_gateway_record_charge(uuid,text,text,numeric,timestamptz)",
        "platform_saas_gateway_record_refund(uuid,text,text,text,numeric,timestamptz)",
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
        "platform_saas_gateway_block_void()",
        "platform_saas_gateway_append_only()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{sig} FROM PUBLIC")


def downgrade() -> None:
    raise RuntimeError("Verified gateway financial history cannot be discarded")
