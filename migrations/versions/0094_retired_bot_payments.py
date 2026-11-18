"""Pay historic ViMi Pay service invoices after retiring the original bot.

Forward-only: the original owner still owes an issued invoice; bot retirement
must not prevent authorized repayment. No new bot or service activation can
be created for a retired bot. Live ownership transfers remain protected.
Existing settlement, idempotency and provider validations are unchanged.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0094_retired_bot_payments"
down_revision: str | None = "0093_saas_external_gateways"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_record_bot_service_activation()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $fn$
DECLARE
  inv record;
  confirmed timestamptz;
  covered_start timestamptz;
  covered_end timestamptz;
  anchor timestamptz;
  prior_anchor timestamptz;
  current_owner uuid;
  following timestamp;
  last_day integer;
  day_of_month integer;
  hhmm interval;
BEGIN
  IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
  SELECT i.* INTO inv FROM public.platform_saas_invoices i
  WHERE i.id=NEW.invoice_id FOR UPDATE;
  IF inv.id IS NULL OR inv.kind<>'prepayment'
     OR inv.status<>'issued' THEN RETURN NEW; END IF;
  -- Finance evidence and live ownership must be serialized. A recipient
  -- cannot buy the same service coverage while the previous owner's
  -- late payment is being confirmed.
  SELECT p.owner_user_id INTO current_owner
  FROM public.bot_instances b
  JOIN public.projects p ON p.id=b.project_id
  WHERE b.id=inv.bot_instance_id
  FOR SHARE OF p;
  -- A retired bot keeps its owner liability but has no service to activate.
  IF current_owner IS NULL THEN RETURN NEW; END IF;
  IF current_owner IS DISTINCT FROM inv.owner_user_id THEN
    -- Verified seller-refund credits may still settle old owner history
    -- without granting service to a bot now owned by someone else.
    IF TG_TABLE_NAME='platform_saas_verified_credit_allocations'
    THEN RETURN NEW; END IF;
    RAISE EXCEPTION
      'Transferred bot invoice requires operator refund reconciliation'
      USING ERRCODE='P6712';
  END IF;
  IF public.platform_saas_v2_remaining(inv.id)>0 THEN RETURN NEW; END IF;
  IF EXISTS(
    SELECT 1 FROM public.platform_saas_bot_service_activations a
    WHERE a.invoice_id=inv.id
  ) THEN RETURN NEW; END IF;
  confirmed:=clock_timestamp();
  SELECT a.anniversary_anchor INTO prior_anchor
  FROM public.platform_saas_bot_service_activations a
  WHERE a.bot_instance_id=inv.bot_instance_id
    AND a.owner_user_id=inv.owner_user_id
    AND a.effective_end<=inv.period_start
  ORDER BY a.effective_end DESC LIMIT 1;
  -- Backward compatibility: older paid periods predate the activation
  -- ledger. Never mistake the current invoice for the prior paid month.
  IF prior_anchor IS NULL THEN
    SELECT min(i.period_start) INTO prior_anchor
    FROM public.platform_saas_invoices i
    WHERE i.bot_instance_id=inv.bot_instance_id
      AND i.owner_user_id=inv.owner_user_id
      AND i.id<>inv.id AND i.kind='prepayment' AND i.status='issued'
      AND i.period_end<=inv.period_start
      AND public.platform_saas_v2_remaining(i.id)=0;
  END IF;
  IF prior_anchor IS NOT NULL AND confirmed<=inv.period_end THEN
    -- Ordinary renewal: retain the original Jan 31 anniversary even
    -- when February's invoice is verified a little after the due time.
    covered_start:=inv.period_start;
    covered_end:=inv.period_end;
    anchor:=prior_anchor;
  ELSE
    -- First post-trial activation OR long-frozen late renewal:
    -- fresh paid coverage begins at verified settlement, not on paper.
    covered_start:=greatest(confirmed,inv.period_start);
    anchor:=covered_start;
    following:=date_trunc('month',anchor AT TIME ZONE 'Europe/Moscow')
               +interval '1 month';
    last_day:=extract(day FROM (
      following+interval '1 month'-interval '1 day'
    )::date)::integer;
    day_of_month:=extract(day FROM
      (anchor AT TIME ZONE 'Europe/Moscow'))::integer;
    hhmm:=(anchor AT TIME ZONE 'Europe/Moscow')-
      date_trunc('day',anchor AT TIME ZONE 'Europe/Moscow');
    covered_end:=(following+make_interval(
      days=>least(day_of_month,last_day)-1
    )+hhmm) AT TIME ZONE 'Europe/Moscow';
  END IF;
  INSERT INTO public.platform_saas_bot_service_activations(
    invoice_id,bot_instance_id,owner_user_id,
    anniversary_anchor,effective_start,effective_end
  ) VALUES(
    inv.id,inv.bot_instance_id,inv.owner_user_id,
    anchor,covered_start,covered_end
  ) ON CONFLICT(invoice_id) DO NOTHING;
  RETURN NEW;
END; $fn$
    """)

    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_stars_create_order(
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
     AND EXISTS(
       SELECT 1 FROM public.bot_instances b
       JOIN public.projects p ON p.id=b.project_id
       WHERE b.id=inv.bot_instance_id
         AND p.owner_user_id IS DISTINCT FROM actor
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
CREATE OR REPLACE FUNCTION public.platform_saas_stars_precheckout(
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
     OR (inv.bot_instance_id IS NOT NULL AND EXISTS(
       SELECT 1 FROM public.bot_instances b
       JOIN public.projects p ON p.id=b.project_id
       WHERE b.id=inv.bot_instance_id
         AND p.owner_user_id IS DISTINCT FROM actor
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
CREATE OR REPLACE FUNCTION public.platform_saas_stars_record_charge(
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
     AND (inv.bot_instance_id IS NULL OR ownership_owner=actor
          OR NOT EXISTS(
            SELECT 1 FROM public.bot_instances b
            WHERE b.id=inv.bot_instance_id
          ))
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
CREATE OR REPLACE FUNCTION public.platform_saas_gateway_reserve(
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
     OR (i.bot_instance_id IS NOT NULL AND EXISTS(
       SELECT 1 FROM public.bot_instances b
       JOIN public.projects p ON p.id=b.project_id
       WHERE b.id=i.bot_instance_id
         AND p.owner_user_id IS DISTINCT FROM actor
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
    -- A charged order is immutable financial evidence, never a new
    -- payment link. Full or partial refund reopens the original debt.
    AND NOT EXISTS(
      SELECT 1 FROM public.platform_saas_gateway_charges c
      WHERE c.order_id=o.id
    )
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
CREATE OR REPLACE FUNCTION public.platform_saas_gateway_record_charge(
  p_order uuid,p_provider text,p_provider_id text,
  p_amount_rub numeric,p_paid_at timestamptz
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE o record; i record; old record; target_invoice uuid;
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
  -- Acquire the invoice lock BEFORE the order lock, exactly like
  -- gateway_reserve. An order-first lock here deadlocks with checkout.
  SELECT invoice_id INTO target_invoice
  FROM public.platform_saas_gateway_orders WHERE id=p_order;
  IF target_invoice IS NULL THEN
    RAISE EXCEPTION 'Provider order does not exist' USING ERRCODE='42501';
  END IF;
  PERFORM 1 FROM public.platform_saas_invoices
    WHERE id=target_invoice FOR UPDATE;
  SELECT * INTO o FROM public.platform_saas_gateway_orders
    WHERE id=p_order FOR UPDATE;
  IF o.id IS NULL OR o.invoice_id<>target_invoice OR o.status<>'issued'
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
     AND (i.bot_instance_id IS NULL OR ownership_owner=o.owner_user_id
          OR NOT EXISTS(
            SELECT 1 FROM public.bot_instances b
            WHERE b.id=i.bot_instance_id
          ))
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


def downgrade() -> None:
    raise RuntimeError(
        "Cannot downgrade historical-invoice payment fix while finance is live"
    )
