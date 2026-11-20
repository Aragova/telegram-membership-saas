"""Opt-in USD-denominated Stars payout quote for ViMi Pay service invoices.

Revision ID: 0098_saas_stars_usd_quote
Revises: 0097_saas_payment_shortlinks

Never reprice issued invoices or historical Stars orders. Future checkout
uses the approved $0.013 reward/Star and an explicit 5% withdrawal/exchange
cost reserve. SQL retains original booked RUB debt for exact-once allocation.
The new rail is OFF until the platform operator explicitly activates it.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0098_saas_stars_usd_quote"
down_revision: str | None = "0097_saas_payment_shortlinks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE public.platform_saas_stars_usd_policy (
  id integer PRIMARY KEY CHECK (id=1),
  usd_per_star numeric(18,8) NOT NULL
    CHECK (usd_per_star BETWEEN 0.005 AND 0.05),
  withdrawal_reserve_pct numeric(5,2) NOT NULL
    CHECK (withdrawal_reserve_pct BETWEEN 0 AND 30),
  enabled boolean NOT NULL DEFAULT false,
  updated_by uuid REFERENCES public.telegram_users(id),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
    """)
    op.execute("""
INSERT INTO public.platform_saas_stars_usd_policy
  (id,usd_per_star,withdrawal_reserve_pct,enabled)
VALUES (1,0.013,5.00,false)
    """)
    op.execute("""
ALTER TABLE public.platform_saas_stars_usd_policy
  ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
ALTER TABLE public.platform_saas_stars_usd_policy
  FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
REVOKE ALL ON TABLE public.platform_saas_stars_usd_policy FROM PUBLIC
    """)
    op.execute("""
DO $r$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
   REVOKE ALL ON TABLE public.platform_saas_stars_usd_policy
     FROM membership_runtime;
 END IF;
END $r$
    """)
    # Nullable additions preserve all already issued or paid 0092 Stars rows.
    op.execute("""
ALTER TABLE public.platform_saas_stars_orders
  ADD COLUMN usd_quote numeric(38,8),
  ADD COLUMN usd_per_star numeric(18,8),
  ADD COLUMN withdrawal_reserve_pct numeric(5,2),
  ADD CONSTRAINT ck_saas_stars_usd_quote_snapshot CHECK (
     (usd_quote IS NULL AND usd_per_star IS NULL
       AND withdrawal_reserve_pct IS NULL)
     OR
     (usd_quote>0 AND usd_per_star>0
       AND withdrawal_reserve_pct BETWEEN 0 AND 30)
  )
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_usd_policy_read()
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE p record;
BEGIN
  PERFORM public.platform_saas_require_owner();
  SELECT * INTO p FROM public.platform_saas_stars_usd_policy WHERE id=1;
  RETURN jsonb_build_object(
    'enabled',p.enabled,'usd_per_star',p.usd_per_star::text,
    'withdrawal_reserve_pct',p.withdrawal_reserve_pct::text,
    'example_usd','5.00',
    'example_stars',ceil(5/(p.usd_per_star*
      (1-p.withdrawal_reserve_pct/100)))::integer
  );
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_usd_policy_set(
  p_usd_per_star numeric,p_withdrawal_reserve_pct numeric,
  p_enabled boolean
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid;
BEGIN
  actor:=public.platform_saas_require_owner();
  IF p_usd_per_star IS NULL
     OR p_usd_per_star NOT BETWEEN 0.005 AND 0.05
     OR p_usd_per_star<>round(p_usd_per_star,8)
     OR p_withdrawal_reserve_pct IS NULL
     OR p_withdrawal_reserve_pct NOT BETWEEN 0 AND 30
     OR p_withdrawal_reserve_pct<>round(p_withdrawal_reserve_pct,2)
     OR p_enabled IS NULL THEN
    RAISE EXCEPTION 'Invalid USD Stars payout policy'
      USING ERRCODE='22023';
  END IF;
  UPDATE public.platform_saas_stars_usd_policy
  SET usd_per_star=p_usd_per_star,
      withdrawal_reserve_pct=p_withdrawal_reserve_pct,
      enabled=p_enabled,updated_by=actor,updated_at=clock_timestamp()
  WHERE id=1;
  -- Return the committed target values, not an earlier STABLE snapshot.
  RETURN jsonb_build_object(
    'enabled',p_enabled,
    'usd_per_star',p_usd_per_star::text,
    'withdrawal_reserve_pct',p_withdrawal_reserve_pct::text,
    'example_usd','5.00',
    'example_stars',ceil(5/(p_usd_per_star*
      (1-p_withdrawal_reserve_pct/100)))::integer
  );
END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_stars_create_order(
  p_invoice uuid,p_telegram_id bigint,p_central_bot bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; inv record; p record; due numeric;
        fee_count integer; fee_ccy text; fee_native numeric; fee_rub numeric;
        fee_usd numeric:=0; commission_rub numeric; fx numeric;
        total_usd numeric; due_usd numeric; qty numeric;
        created uuid; payload text; expiry timestamptz;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  IF actor IS NULL OR p_telegram_id IS NULL OR p_telegram_id<=0
     OR p_central_bot IS NULL OR p_central_bot<=0
     OR NOT EXISTS(SELECT 1 FROM public.telegram_users u
       WHERE u.id=actor AND u.telegram_user_id=p_telegram_id) THEN
    RAISE EXCEPTION 'Stars payer identity mismatch' USING ERRCODE='42501';
  END IF;
  SELECT * INTO inv FROM public.platform_saas_invoices
    WHERE id=p_invoice FOR UPDATE;
  IF inv.id IS NULL OR inv.status<>'issued'
     OR inv.owner_user_id IS DISTINCT FROM actor THEN
    RAISE EXCEPTION 'Invoice no longer payable' USING ERRCODE='42501';
  END IF;
  -- A retired bot has no live row but keeps its original owner's debt.
  -- Reject only a live transfer to a different owner (0094 contract).
  IF inv.bot_instance_id IS NOT NULL
     AND EXISTS(
       SELECT 1 FROM public.bot_instances b
       JOIN public.projects pr ON pr.id=b.project_id
       WHERE b.id=inv.bot_instance_id
         AND pr.owner_user_id IS DISTINCT FROM actor
     ) THEN
    RAISE EXCEPTION 'Transferred invoice requires reconciliation'
      USING ERRCODE='42501';
  END IF;
  SELECT * INTO p FROM public.platform_saas_stars_usd_policy WHERE id=1;
  IF p.enabled IS DISTINCT FROM true OR p.usd_per_star IS NULL
     OR p.withdrawal_reserve_pct IS NULL THEN
    RAISE EXCEPTION 'Operator USD Stars payout policy is not active'
      USING ERRCODE='P6713';
  END IF;
  due:=public.platform_saas_v2_remaining(inv.id);
  IF due<=0 OR inv.total_rub<=0 THEN
    RAISE EXCEPTION 'Invoice already settled' USING ERRCODE='23514';
  END IF;
  SELECT count(*),max(original_currency),max(original_amount),
         max(charge_rub)
    INTO fee_count,fee_ccy,fee_native,fee_rub
  FROM public.platform_saas_invoice_lines
  WHERE invoice_id=inv.id AND source_kind='bot_fee';
  IF fee_count>1 THEN
    RAISE EXCEPTION 'Ambiguous original service fee' USING ERRCODE='23514';
  ELSIF fee_count=1 THEN
    IF fee_ccy='RUB' AND fee_native=150 AND fee_rub=150 THEN
      -- The owner chose a $5 Stars payout-priced alternative to RUB 150.
      fee_usd:=5;
    ELSIF fee_ccy='USD' AND fee_native IN (3,5) AND fee_rub>0 THEN
      -- Honor old issued USD $3 invoices; never retroactively reprice them.
      fee_usd:=fee_native;
    ELSE
      RAISE EXCEPTION 'Invalid immutable service price snapshot'
        USING ERRCODE='23514';
    END IF;
  END IF;
  IF fee_rub IS NOT NULL AND fee_rub>inv.total_rub THEN
    RAISE EXCEPTION 'Invalid invoice service amount' USING ERRCODE='23514';
  END IF;
  commission_rub:=inv.total_rub-coalesce(fee_rub,0);
  IF commission_rub<0 THEN
    RAISE EXCEPTION 'Invalid commission allocation' USING ERRCODE='23514';
  END IF;
  IF commission_rub>0 THEN
    -- A valid RUB service invoice may have been issued before its exact-day
    -- CBR snapshot was published. Use the most recent previously effective
    -- verified CBR rate, never future data or an unbounded stale quote.
    -- The original immutable RUB debt is still credited in full on payment.
    SELECT rub_per_unit INTO fx
    FROM public.platform_saas_daily_fx_rates
    WHERE currency_code='USD' AND source_name='CBR'
      AND rate_date<=(inv.created_at AT TIME ZONE 'Europe/Moscow')::date
      AND rate_date>=(inv.created_at AT TIME ZONE 'Europe/Moscow')::date
                       -interval '14 days'
      AND source_date<=(inv.created_at AT TIME ZONE 'Europe/Moscow')::date
      AND source_date>=(inv.created_at AT TIME ZONE 'Europe/Moscow')::date
                         -interval '14 days'
      AND source_date<=rate_date
    ORDER BY rate_date DESC,source_date DESC LIMIT 1;
    IF fx IS NULL OR fx<=0 THEN
      RAISE EXCEPTION 'Verified USD commission FX unavailable'
        USING ERRCODE='42501';
    END IF;
  END IF;
  total_usd:=fee_usd+
    CASE WHEN commission_rub>0 THEN commission_rub/fx ELSE 0 END;
  IF total_usd<=0 THEN
    RAISE EXCEPTION 'Missing priced invoice lines' USING ERRCODE='23514';
  END IF;
  -- Existing verified receipts and credits shrink ONLY the unpaid portion.
  -- Quote is USD-denominated; remaining RUB is used solely for book credit.
  due_usd:=round(total_usd*due/inv.total_rub,8);
  qty:=ceil(due_usd/(p.usd_per_star*(1-p.withdrawal_reserve_pct/100)));
  IF qty<1 OR qty>250000 THEN
    RAISE EXCEPTION 'Invoice outside Telegram Stars checkout range'
      USING ERRCODE='22023';
  END IF;
  created:=gen_random_uuid();
  payload:='vps:'||created::text;
  expiry:=clock_timestamp()+interval '20 minutes';
  INSERT INTO public.platform_saas_stars_orders (
    id,invoice_id,owner_user_id,payer_telegram_id,central_bot_id,
    invoice_payload,rub_amount,stars_amount,rub_per_star,expires_at,
    usd_quote,usd_per_star,withdrawal_reserve_pct
  ) VALUES (
    created,p_invoice,actor,p_telegram_id,p_central_bot,
    payload,due,qty::integer,round(due/qty,18),expiry,
    due_usd,p.usd_per_star,p.withdrawal_reserve_pct
  );
  RETURN jsonb_build_object(
    'order_id',created,'payload',payload,'stars_amount',qty::integer,
    'rub_amount',due::text,'usd_quote',due_usd::text,
    'usd_per_star',p.usd_per_star::text,
    'withdrawal_reserve_pct',p.withdrawal_reserve_pct::text,
    'expires_at',expiry
  );
END; $f$
    """)
    # Preserve original charge/refund/activation evidence logic. Existing
    # XTR orders preserve their previous precheckout semantics.
    op.execute("""\nCREATE OR REPLACE FUNCTION public.platform_saas_stars_precheckout(
  p_payload text,p_telegram_id bigint,p_bot_id bigint,p_amount integer
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; ord record; inv record; moment timestamptz;
        usd_policy_enabled boolean;
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
  IF ord.usd_quote IS NOT NULL THEN
    -- Serialize authorization with the operator's disabling UPDATE.
    -- If suspension commits first, this SELECT sees the disabled row;
    -- otherwise suspension waits until this checkout commits.
    SELECT enabled INTO usd_policy_enabled
    FROM public.platform_saas_stars_usd_policy
    WHERE id=1 FOR SHARE;
  END IF;
  IF inv.id IS NULL OR inv.status<>'issued'
     OR inv.owner_user_id IS DISTINCT FROM actor
     OR moment>ord.expires_at
     -- Emergency suspension blocks new USD precheckouts under a held
     -- SHARE lock; historical XTR orders retain their prior semantics.
     OR (ord.usd_quote IS NOT NULL
         AND usd_policy_enabled IS DISTINCT FROM true)
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
END; $f$\n    """)

    for sig in (
        "platform_saas_stars_usd_policy_read()",
        "platform_saas_stars_usd_policy_set(numeric,numeric,boolean)",
        "platform_saas_stars_create_order(uuid,bigint,bigint)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{sig} FROM PUBLIC")
        op.execute(f"""
DO $r$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
   GRANT EXECUTE ON FUNCTION public.{sig} TO membership_runtime;
 END IF;
END $r$
        """)


def downgrade() -> None:
    raise RuntimeError("Refuse to discard immutable Stars USD quotes and receipts")
