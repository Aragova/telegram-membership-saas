"""Immutable native RUB/USD provider checkout quotes tied to one bot invoice.

Revision ID: 0100_saas_native_gateway
Revises: 0099_saas_bot_currency_stars
Each provider's original native amount is verified before the existing
book-RUB ledger is credited. Prior provider orders retain RUB snapshots.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0100_saas_native_gateway"
down_revision: str | None = "0099_saas_bot_currency_stars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
ALTER TABLE public.platform_saas_gateway_orders
 ADD COLUMN pay_currency text NOT NULL DEFAULT 'RUB',
 ADD COLUMN pay_amount numeric(38,2)
    """)
    op.execute("""
UPDATE public.platform_saas_gateway_orders
 SET pay_amount=amount_rub WHERE pay_amount IS NULL
    """)
    op.execute("""
ALTER TABLE public.platform_saas_gateway_orders
 ALTER COLUMN pay_amount SET NOT NULL,
 ADD CONSTRAINT ck_saas_gateway_native_amount CHECK(
   pay_currency IN ('RUB','USD') AND pay_amount>0
 )
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_gateway_reserve(
  p_invoice uuid,p_provider text,p_telegram_id bigint,p_bot_id bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; i record; prior record; due numeric;
        created uuid; payload text; expiry timestamptz;
        currency text:='RUB'; pay_amount numeric; usd_rate numeric;
        fee_ccy text; fee_native numeric; fee_rub numeric;
        already_paid numeric; still_service numeric; other_rub numeric;
        payment_day date;
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
  IF due<=0 THEN
    RAISE EXCEPTION 'No outstanding original invoice balance'
      USING ERRCODE='22023';
  END IF;
  -- Original issued bot fee is the ONLY authority for payment currency.
  -- RUB debts remain RUB. USD debts retain their original native service.
  SELECT original_currency,original_amount,charge_rub
  INTO fee_ccy,fee_native,fee_rub
  FROM public.platform_saas_invoice_lines
  WHERE invoice_id=i.id AND source_kind='bot_fee' LIMIT 1;
  currency:=CASE WHEN fee_ccy='USD' THEN 'USD' ELSE 'RUB' END;
  pay_amount:=due;
  IF currency='USD' THEN
    IF fee_native IS NULL OR fee_native<=0 OR fee_rub IS NULL
       OR fee_rub<=0 THEN
      RAISE EXCEPTION 'Invalid frozen service line'
        USING ERRCODE='23514';
    END IF;
    already_paid:=i.total_rub-due;
    still_service:=greatest(0,least(due,fee_rub-already_paid));
    other_rub:=greatest(0,due-still_service);
    IF other_rub>0 THEN
      payment_day:=(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date;
      SELECT rub_per_unit INTO usd_rate
      FROM public.platform_saas_daily_fx_rates
      WHERE currency_code='USD' AND source_name='CBR'
        AND rate_date<=payment_day
        AND rate_date>=payment_day-interval '14 days'
        AND source_date<=payment_day
        AND source_date>=payment_day-interval '14 days'
        AND source_date<=rate_date
      ORDER BY rate_date DESC,source_date DESC LIMIT 1;
      IF usd_rate IS NULL OR usd_rate<=0 THEN
        RAISE EXCEPTION 'Verified checkout USD conversion unavailable'
          USING ERRCODE='42501';
      END IF;
    END IF;
    pay_amount:=ceil((
      fee_native*still_service/fee_rub
      + CASE WHEN other_rub>0 THEN other_rub/usd_rate ELSE 0 END
    )*100)/100;
  END IF;
  -- Do not inflate provider amounts simply to satisfy a minimum.
  IF pay_amount<=0 OR (p_provider='lava' AND (
    (currency='USD' AND pay_amount<5)
    OR (currency='RUB' AND pay_amount<50))) THEN
    RAISE EXCEPTION 'Outstanding balance below provider minimum'
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
      'pay_currency',prior.pay_currency,'pay_amount',prior.pay_amount::text,
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
    provider,invoice_payload,amount_rub,pay_currency,pay_amount,expires_at
  ) VALUES(
    created,i.id,actor,p_telegram_id,p_bot_id,p_provider,
    payload,due,currency,pay_amount,expiry
  );
  RETURN jsonb_build_object(
    'id',created,'payload',payload,'status','reserved',
    'amount_rub',due::text,'pay_currency',currency,
    'pay_amount',pay_amount::text,'provider_invoice_id',NULL,
    'url',NULL,'expires_at',expiry
  );
END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_gateway_pending(p_limit integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE selected jsonb;
BEGIN
  PERFORM public.platform_saas_require_owner();
  IF p_limit IS NULL OR p_limit<1 OR p_limit>100 THEN
    RAISE EXCEPTION 'Invalid gateway page size' USING ERRCODE='22023';
  END IF;
  -- Fair, crash-safe round robin: 100 old unpaid invoices must not
  -- starve all newer paid invoices. Record the scan lease before the
  -- provider request, and revisit all unpaid rows on later scans.
  WITH candidates AS MATERIALIZED (
    SELECT o.id
    FROM public.platform_saas_gateway_orders o
    WHERE o.status='issued'
      AND NOT EXISTS(
        SELECT 1 FROM public.platform_saas_gateway_charges c
        WHERE c.order_id=o.id
      )
    ORDER BY o.last_rechecked_at NULLS FIRST,o.created_at ASC,o.id
    FOR UPDATE OF o SKIP LOCKED LIMIT p_limit
  ), updated AS (
    UPDATE public.platform_saas_gateway_orders o
    SET last_rechecked_at=clock_timestamp()
    FROM candidates c WHERE c.id=o.id
    RETURNING o.id,o.invoice_id,o.owner_user_id,o.payer_telegram_id,
      o.central_bot_id,o.provider,o.invoice_payload,
      o.amount_rub::text AS amount_rub,
      o.pay_currency,o.pay_amount::text AS pay_amount,
      o.provider_invoice_id,
      o.created_at
  )
  SELECT coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)
    INTO selected FROM updated x;
  RETURN selected;
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_quote_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
BEGIN
 IF (NEW.pay_currency,NEW.pay_amount)
     IS DISTINCT FROM (OLD.pay_currency,OLD.pay_amount) THEN
   RAISE EXCEPTION 'Provider native checkout quote is immutable'
     USING ERRCODE='42501';
 END IF;
 RETURN NEW;
END; $f$
    """)
    op.execute("""
CREATE TRIGGER trg_saas_gateway_native_immutable
BEFORE UPDATE OF pay_currency,pay_amount
ON public.platform_saas_gateway_orders
FOR EACH ROW EXECUTE FUNCTION
 public.platform_saas_gateway_quote_immutable()
    """)
    op.execute("""
REVOKE ALL ON FUNCTION
 public.platform_saas_gateway_quote_immutable() FROM PUBLIC
    """)


def downgrade() -> None:
    raise RuntimeError("Provider payment evidence and immutable quotes cannot be reverted")
