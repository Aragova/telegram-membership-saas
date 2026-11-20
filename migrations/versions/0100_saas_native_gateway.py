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
  -- Existing unexpired provider orders must be honoured at their frozen
  -- original native price, even for historical USD3 Lava invoices that
  -- cannot meet today's USD5 new-payment minimum.
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
  -- Original issued bot fee is the ONLY authority for payment currency.
  -- RUB debts remain RUB. USD debts retain their original native service.
  SELECT original_currency,original_amount,charge_rub,rub_per_unit
  INTO fee_ccy,fee_native,fee_rub,usd_rate
  FROM public.platform_saas_invoice_lines
  WHERE invoice_id=i.id AND source_kind='bot_fee' LIMIT 1;
  currency:=CASE WHEN fee_ccy='USD' THEN 'USD' ELSE 'RUB' END;
  pay_amount:=due;
  IF currency='USD' THEN
    IF fee_native IS NULL OR fee_native<=0 OR fee_rub IS NULL
       OR fee_rub<=0 OR usd_rate IS NULL OR usd_rate<=0 THEN
      RAISE EXCEPTION 'Invalid frozen service line'
        USING ERRCODE='23514';
    END IF;
    already_paid:=i.total_rub-due;
    still_service:=greatest(0,least(due,fee_rub-already_paid));
    other_rub:=greatest(0,due-still_service);
    -- The original USD invoice included its sales commission valued
    -- at the CBR rate frozen on ISSUE. Paying later cannot reprice it.
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

    # Existing native USD invoices cannot drift with later CBR updates even
    # when paid through the manually reviewed bank payment catalog.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v3_create_attempt(
      p_invoice uuid,p_requested_rub numeric,p_currency text,p_method uuid
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE
      actor uuid;
      item record;
      paid numeric;
      due numeric;
      rate numeric;
      fx_day date;
      day date;
      bank text;
      method_label text;
      converted numeric;
      snapped_rub numeric;
      attempt uuid;
      deadline timestamptz;
      native_usd numeric;
      base_service_rub numeric;
      remaining_service_rub numeric;
      remaining_other_rub numeric;
      remaining_native_usd numeric;
      usd_rate_today numeric;
      issue_usd_fx numeric;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      IF actor IS NULL OR p_invoice IS NULL
         OR p_currency NOT IN ('RUB','USD','EUR','UAH','BYN')
         OR p_requested_rub IS NULL OR p_requested_rub<=0
         OR p_requested_rub<>round(p_requested_rub,2) THEN
        RAISE EXCEPTION 'Invalid payment amount/currency'
          USING ERRCODE='22023';
      END IF;
      PERFORM public.platform_saas_reconcile_verified_refunds(actor);
      SELECT * INTO item FROM public.platform_saas_invoices
        WHERE id=p_invoice FOR UPDATE;
      IF item.id IS NULL OR item.status<>'issued'
         OR item.owner_user_id IS DISTINCT FROM actor THEN
        RAISE EXCEPTION 'Account invoice is not payable'
          USING ERRCODE='42501';
      END IF;
      SELECT coalesce(sum(amount_rub),0) INTO paid
      FROM public.platform_saas_receipts WHERE invoice_id=p_invoice;
      SELECT paid+coalesce(sum(allocated_rub),0) INTO paid
      FROM public.platform_saas_payment_allocations_v2
      WHERE invoice_id=p_invoice;
      due:=public.platform_saas_v2_remaining(item.id);
      IF p_requested_rub<>due THEN
        RAISE EXCEPTION 'Only full remaining invoice payment is supported'
          USING ERRCODE='23514';
      END IF;
      -- The chosen method and currency must still be active, and the
      -- immutable attempt snapshots the exact central method instructions.
      SELECT m.instructions,m.name INTO bank,method_label
      FROM public.platform_saas_payment_catalog_methods m
      JOIN public.platform_saas_payment_catalog_currencies c
        ON c.method_id=m.id AND c.currency_code=p_currency
      WHERE m.id=p_method AND m.enabled AND m.removed_at IS NULL
      FOR SHARE OF m;
      IF bank IS NULL THEN
        RAISE EXCEPTION 'Platform bank details not published'
          USING ERRCODE='42501';
      END IF;
      day:=(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date;
      IF p_currency='RUB' THEN rate:=1;fx_day:=NULL;
      ELSE
        SELECT rub_per_unit,source_date INTO rate,fx_day
        FROM public.platform_saas_daily_fx_rates
        WHERE rate_date=day AND currency_code=p_currency
          AND source_name='CBR' AND source_date<=day
          AND source_date>=day-interval '14 days';
      END IF;
      IF rate IS NULL OR rate<=0 THEN
        RAISE EXCEPTION 'Verified bank currency conversion unavailable'
          USING ERRCODE='42501';
      END IF;
      -- Foreign currency is paid in 2 decimal places. A rounded bank
      -- transfer can differ slightly from the RUB obligation. Preserve the
      -- exact RUB amount requested; the platform absorbs that bounded FX
      -- rounding difference rather than creating an unpayable residual.
      -- Preserve the original $3 service obligation across FX dates.
      -- RUB allocations remain at their immutable issue-day book value;
      -- only the actual bank transfer quote uses the current rate.
      SELECT l.original_amount,l.charge_rub,l.rub_per_unit
      INTO native_usd,base_service_rub,issue_usd_fx
      FROM public.platform_saas_invoice_lines l
      WHERE l.invoice_id=item.id AND l.source_kind='bot_fee'
        AND l.original_currency='USD'
      LIMIT 1;
      IF native_usd IS NOT NULL THEN
        -- Confirmed bank receipts reduce the native $3 service portion
        -- first at its original book value. Verified provider refunds are
        -- separate RUB credits, not bank cash; they reduce the remaining
        -- invoice balance. Clamp the service portion to that balance so
        -- carried credits cannot cause an overcharge or negative quote.
        IF base_service_rub IS NULL OR base_service_rub<=0
           OR native_usd<=0 OR issue_usd_fx IS NULL
           OR issue_usd_fx<=0 THEN
          RAISE EXCEPTION 'Invalid original USD service snapshot'
            USING ERRCODE='23514';
        END IF;
        remaining_service_rub:=greatest(0,least(
          due,base_service_rub,
          base_service_rub-paid
        ));
        remaining_other_rub:=greatest(0,due-remaining_service_rub);
        remaining_native_usd:=native_usd*remaining_service_rub/base_service_rub;
      END IF;
      IF native_usd IS NULL THEN
        converted:=round(p_requested_rub/rate,2);
      ELSIF p_currency='USD' THEN
        -- The issued USD invoice has ONE immutable USD total, even if
        -- the payer uses the manual rail days after its issuance.
        converted:=round(
          remaining_native_usd+remaining_other_rub/issue_usd_fx,2
        );
      ELSE
        SELECT rub_per_unit INTO usd_rate_today
        FROM public.platform_saas_daily_fx_rates
        WHERE rate_date=day AND currency_code='USD'
          AND source_name='CBR' AND source_date<=day
          AND source_date>=day-interval '14 days';
        IF usd_rate_today IS NULL OR usd_rate_today<=0 THEN
          RAISE EXCEPTION 'Current USD service quote unavailable'
            USING ERRCODE='42501';
        END IF;
        -- All components of an issued USD invoice are frozen in USD.
        -- Convert the ENTIRE remaining native USD obligation to the
        -- chosen payment currency at payment-day verified CBR rates.
        -- Applying today's USD rate only to service and leaving the
        -- RUB-booked commission unchanged would under/overcharge.
        converted:=round(
          (remaining_native_usd+remaining_other_rub/issue_usd_fx)
            *usd_rate_today/rate,2
        );
      END IF;
      snapped_rub:=p_requested_rub;
      IF converted<=0 OR snapped_rub>due
         OR (native_usd IS NULL AND
             abs(converted*rate-snapped_rub)>rate*0.005+0.005) THEN
        RAISE EXCEPTION 'Converted payment amount outside remaining debt'
          USING ERRCODE='23514';
      END IF;
      deadline:=clock_timestamp()+interval '48 hours';
      attempt:=gen_random_uuid();
      INSERT INTO public.platform_saas_payment_attempts_v2(
        id,invoice_id,owner_user_id,amount_rub,currency_code,
        rub_per_unit,amount_currency,quote_date,source_date,
        instructions_snapshot,expires_at,method_id
      ) VALUES(
        attempt,item.id,actor,snapped_rub,p_currency,rate,
        converted,day,fx_day,bank,deadline,p_method
      );
      RETURN jsonb_build_object(
        'id',attempt,'invoice_id',item.id,'amount_rub',snapped_rub::text,
        'currency',p_currency,'amount',converted::text,
        'rub_per_unit',rate::text,'quote_date',day,
        'source_date',fx_day,'expires_at',deadline,
        'instructions',bank,'method_name',method_label,
        'method_id',p_method,'remaining_before',due::text
      );
    END; $f$
    """)

def downgrade() -> None:
    raise RuntimeError("Provider payment evidence and immutable quotes cannot be reverted")
