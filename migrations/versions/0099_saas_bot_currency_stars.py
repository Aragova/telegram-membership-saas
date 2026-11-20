"""Bot-scoped ViMi Pay service currency and faithful Stars quote.

Revision ID: 0099_saas_bot_currency_stars
Revises: 0098_saas_stars_usd_quote

No historical bill is edited. All currency profiles are prospective for one
registered bot, and the original immutable invoice remains the only debt.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0099_saas_bot_currency_stars"
down_revision: str | None = "0098_saas_stars_usd_quote"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE public.platform_saas_bot_price_profiles (
  bot_instance_id uuid PRIMARY KEY,
  owner_user_id uuid NOT NULL REFERENCES public.telegram_users(id),
  currency_code text NOT NULL CHECK (currency_code IN ('RUB','USD')),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
    """)
    op.execute("""
ALTER TABLE public.platform_saas_bot_price_profiles ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
ALTER TABLE public.platform_saas_bot_price_profiles FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
REVOKE ALL ON TABLE public.platform_saas_bot_price_profiles FROM PUBLIC
    """)
    op.execute("""
DO $r$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
  REVOKE ALL ON TABLE public.platform_saas_bot_price_profiles
    FROM membership_runtime;
 END IF;
END $r$
    """)
    # Snapshot all existing registered bots BEFORE changing the issuer.
    # New registration / ownership changes preserve one default per bot.
    op.execute("""
INSERT INTO public.platform_saas_bot_price_profiles(
  bot_instance_id,owner_user_id,currency_code)
SELECT b.id,p.owner_user_id,coalesce(
 (SELECT pp.currency_code FROM public.platform_saas_price_profiles pp
  WHERE pp.owner_user_id=p.owner_user_id),'RUB')
FROM public.bot_instances b
JOIN public.projects p ON p.id=b.project_id
WHERE b.status='registered'
ON CONFLICT(bot_instance_id) DO NOTHING
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_bot_profile_on_registration()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE own uuid; cur text;
BEGIN
 IF NEW.status='registered'
    AND (TG_OP='INSERT' OR OLD.status IS DISTINCT FROM NEW.status)
 THEN
   SELECT p.owner_user_id INTO own
   FROM public.projects p WHERE p.id=NEW.project_id;
   IF own IS NULL THEN
     RAISE EXCEPTION 'Registered bot has no project owner'
       USING ERRCODE='42501';
   END IF;
   SELECT coalesce(pp.currency_code,'RUB') INTO cur
   FROM (SELECT 1) d LEFT JOIN public.platform_saas_price_profiles pp
     ON pp.owner_user_id=own;
   INSERT INTO public.platform_saas_bot_price_profiles(
     bot_instance_id,owner_user_id,currency_code)
   VALUES(NEW.id,own,cur)
   ON CONFLICT(bot_instance_id) DO NOTHING;
 END IF;
 RETURN NEW;
END; $f$
    """)
    op.execute("""
CREATE TRIGGER trg_saas_bot_register_price
AFTER INSERT OR UPDATE OF status ON public.bot_instances
FOR EACH ROW EXECUTE FUNCTION public.platform_saas_bot_profile_on_registration()
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_bot_profile_on_owner_change()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE cur text;
BEGIN
 IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id THEN
   SELECT coalesce((SELECT pp.currency_code
     FROM public.platform_saas_price_profiles pp
     WHERE pp.owner_user_id=NEW.owner_user_id),'RUB')
     INTO cur;
   UPDATE public.platform_saas_bot_price_profiles bp
   SET owner_user_id=NEW.owner_user_id,currency_code=cur,
       updated_at=clock_timestamp()
   FROM public.bot_instances b
   WHERE bp.bot_instance_id=b.id AND b.project_id=NEW.id
     AND b.status='registered';
 END IF;
 RETURN NEW;
END; $f$
    """)
    op.execute("""
CREATE TRIGGER trg_saas_bot_price_owner_changed
AFTER UPDATE OF owner_user_id ON public.projects
FOR EACH ROW EXECUTE FUNCTION public.platform_saas_bot_profile_on_owner_change()
    """)
    for sig in (
        "platform_saas_bot_profile_on_registration()",
        "platform_saas_bot_profile_on_owner_change()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{sig} FROM PUBLIC")
    op.execute("""
CREATE FUNCTION public.platform_saas_bot_price_profile_read(p_project uuid)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; bot_id uuid; cur text;
BEGIN
 actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 SELECT b.id INTO STRICT bot_id
 FROM public.bot_instances b JOIN public.projects p ON p.id=b.project_id
 WHERE b.project_id=p_project AND b.status='registered'
   AND p.owner_user_id=actor;
 SELECT bp.currency_code INTO cur
 FROM public.platform_saas_bot_price_profiles bp
 WHERE bp.bot_instance_id=bot_id AND bp.owner_user_id=actor;
 IF cur IS NULL THEN
   RAISE EXCEPTION 'Bot service currency was not initialized'
     USING ERRCODE='42501';
 END IF;
 RETURN jsonb_build_object('currency',cur,'bot_id',bot_id,
   'monthly_amount',CASE WHEN cur='USD' THEN '5.00'
                         ELSE '150.00' END);
EXCEPTION WHEN NO_DATA_FOUND OR TOO_MANY_ROWS THEN
 RAISE EXCEPTION 'Exactly one registered bot owned in project is required'
  USING ERRCODE='42501';
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_bot_price_profile_set(
  p_project uuid,p_currency text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; bot_id uuid;
BEGIN
 actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 IF actor IS NULL OR p_currency NOT IN ('RUB','USD') THEN
  RAISE EXCEPTION 'Invalid bot service currency' USING ERRCODE='42501';
 END IF;
 -- Lock the project ownership row before authorizing currency changes.
 -- Transfers UPDATE this same project row before their price-profile
 -- trigger. Holding FOR SHARE serializes setter vs transfer; neither
 -- the former owner nor a stale actor can overwrite a new owner.
 -- Lock order: project -> bot advisory -> bot price profile.
 SELECT b.id INTO STRICT bot_id
 FROM public.projects p JOIN public.bot_instances b ON b.project_id=p.id
 WHERE p.id=p_project AND b.status='registered'
   AND p.owner_user_id=actor FOR SHARE OF p;
 PERFORM pg_advisory_xact_lock(
   hashtextextended('saas:bot:'||bot_id::text,0));
 IF EXISTS(
   SELECT 1 FROM public.platform_saas_invoices i
   WHERE i.bot_instance_id=bot_id AND i.owner_user_id=actor
    AND i.kind='prepayment' AND i.status='issued'
    AND public.platform_saas_v2_remaining(i.id)>0
 ) THEN
  RAISE EXCEPTION 'Settle outstanding service invoice before changing profile'
   USING ERRCODE='23514';
 END IF;
 INSERT INTO public.platform_saas_bot_price_profiles(
   bot_instance_id,owner_user_id,currency_code)
 VALUES(bot_id,actor,p_currency)
 ON CONFLICT(bot_instance_id) DO UPDATE SET
   owner_user_id=excluded.owner_user_id,
   currency_code=excluded.currency_code,
   updated_at=clock_timestamp();
 -- Return the freshly written value (a STABLE reader may see the
 -- pre-write statement snapshot inside a setter transaction).
 RETURN jsonb_build_object(
   'currency',p_currency,'bot_id',bot_id,
   'monthly_amount',CASE WHEN p_currency='USD' THEN '5.00'
                         ELSE '150.00' END);
EXCEPTION WHEN NO_DATA_FOUND OR TOO_MANY_ROWS THEN
 RAISE EXCEPTION 'Exactly one registered bot owned in project is required'
  USING ERRCODE='42501';
END; $f$
    """)
    for sig in (
        "platform_saas_bot_price_profile_read(uuid)",
        "platform_saas_bot_price_profile_set(uuid,text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{sig} FROM PUBLIC")
        op.execute(f"""
DO $r$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
  GRANT EXECUTE ON FUNCTION public.{sig} TO membership_runtime;
 END IF;
END $r$
        """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_price_profile_set(p_currency text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      IF actor IS NULL OR p_currency NOT IN ('RUB','USD') THEN
        RAISE EXCEPTION 'Invalid account price profile'
          USING ERRCODE='42501';
      END IF;
      PERFORM pg_advisory_xact_lock(hashtextextended(
        'vimi:saas:price:'||actor::text,0
      ));
      -- Existing bots have immutable prospective profile snapshots.
      -- Account default affects future newly registered bots only.
      INSERT INTO public.platform_saas_price_profiles(
        owner_user_id,currency_code
      ) VALUES(actor,p_currency)
      ON CONFLICT(owner_user_id) DO UPDATE SET
        currency_code=excluded.currency_code,updated_at=clock_timestamp();
      RETURN public.platform_saas_price_profile_read();
    END; $f$
    """)
    op.execute("""
ALTER TABLE public.platform_saas_stars_orders
  ADD COLUMN payment_surcharge_pct numeric(5,2)
  CHECK (payment_surcharge_pct BETWEEN 0 AND 30)
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_issue_bot_invoice(
            p_bot uuid, p_start timestamptz, p_end timestamptz
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          first_charge timestamptz;
          entry record;
          trial_started timestamptz;
          invoice_id uuid;
          invoice_status text;
          exemption text;
          service_key text;
          currency text;
          amount_native numeric;
          invoice_rub numeric;
          usd_rate numeric;
          issue_at timestamptz;
          prior_service record;
          quote jsonb := '[]'::jsonb;
          datum jsonb;
          combined_fee numeric := 0;
          base_fee numeric;
        BEGIN
          first_charge := public.platform_saas_require_rollout();
          IF p_bot IS NULL OR p_start IS NULL OR p_end IS NULL
             OR p_start<first_charge OR p_end IS DISTINCT FROM
                public.platform_saas_bot_period_end(p_bot,p_start)
             OR p_end<=p_start OR p_start>clock_timestamp() THEN
            RAISE EXCEPTION 'Invalid service period or rollout date'
              USING ERRCODE='22023';
          END IF;
          PERFORM pg_advisory_xact_lock(
            hashtextextended('saas:bot:'||p_bot::text,0)
          );
          SELECT b.project_id, p.merchant_id, p.owner_user_id,
                 b.created_at AS connected_at
          INTO entry FROM public.bot_instances b
          JOIN public.projects p ON p.id=b.project_id
          WHERE b.id=p_bot AND b.status='registered';
          IF NOT FOUND OR p_start<entry.connected_at THEN
            RAISE EXCEPTION 'Connected billable bot required'
              USING ERRCODE='22023';
          END IF;
          -- Only the owner who actually held this project for the ENTIRE
          -- service period may be charged. A mid-period transfer requires
          -- operator resolution, not an invoice against the new owner.
          IF NOT EXISTS(
            SELECT 1 FROM public.merchant_project_periods mp
            WHERE mp.project_id=entry.project_id
              AND mp.merchant_id=entry.merchant_id
              AND mp.owner_user_id=entry.owner_user_id
              AND mp.started_at<=p_start
              AND (mp.ended_at IS NULL OR mp.ended_at>=p_end)
          ) THEN
            RAISE EXCEPTION 'Service period owner attribution needs review'
              USING ERRCODE='42501';
          END IF;
          -- A platform owner may administer third-party channels.
          -- Never treat DB owner role as proof of channel creator status.
          -- A matching creator-verified record is required to waive fees;
          -- without one, block billing pending an ownership review.
          IF EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=entry.owner_user_id AND o.role='owner'
          ) THEN
            exemption := public.platform_saas_exemption_state(
                entry.project_id,p_bot,entry.merchant_id,
                entry.owner_user_id,p_start);
            IF exemption='exempt' THEN RETURN NULL; END IF;
            RAISE EXCEPTION 'Platform-admin project requires creator review'
              USING ERRCODE='42501';
          END IF;
          SELECT min(claimed_at) INTO trial_started
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=entry.owner_user_id
            AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';
          IF trial_started IS NULL
             OR p_start<public.platform_saas_billable_after(entry.owner_user_id) THEN
            RAISE EXCEPTION 'Owner trial state is not billable yet'
              USING ERRCODE='42501';
          END IF;
          IF public.platform_saas_is_transition_owner(entry.owner_user_id)
             AND p_start>clock_timestamp() THEN
            RAISE EXCEPTION 'Transition service invoice not yet due'
              USING ERRCODE='42501';
          END IF;
          exemption := public.platform_saas_exemption_state(
              entry.project_id,p_bot,entry.merchant_id,entry.owner_user_id,p_start);
          IF exemption='exempt' THEN RETURN NULL; END IF;
          IF exemption<>'not_exempt' THEN
            RAISE EXCEPTION 'Channel exemption needs manual verification'
              USING ERRCODE='42501';
          END IF;
          -- UTC rendering is canonical across DB session TimeZone values.
          service_key := 'bot:'||p_bot::text||':'||
                         to_char(p_start AT TIME ZONE 'UTC',
                                 'YYYYMMDD"T"HH24MISS.US');
          -- Prevent overlapping monthly charges for the same connection.
          IF EXISTS(
            SELECT 1 FROM public.platform_saas_invoices prior
            LEFT JOIN public.platform_saas_bot_service_activations coverage
              ON coverage.invoice_id=prior.id
            WHERE prior.bot_instance_id=p_bot
              AND prior.kind='prepayment' AND prior.status<>'void'
              AND prior.invoice_key<>service_key
              AND tstzrange(
                prior.period_start,
                greatest(prior.period_end,
                  coalesce(coverage.effective_end,prior.period_end)),
                '[)'
              ) && tstzrange(p_start,p_end,'[)')
          ) THEN
            RAISE EXCEPTION 'Overlapping service invoices are forbidden'
              USING ERRCODE='23514';
          END IF;
          SELECT i.id,i.status INTO invoice_id,invoice_status
            FROM public.platform_saas_invoices i
            WHERE i.invoice_key=service_key FOR UPDATE;
          IF invoice_id IS NOT NULL THEN
            IF invoice_status<>'issued' THEN
              RAISE EXCEPTION 'Existing invoice is not issued'
                USING ERRCODE='42501';
            END IF;
            RETURN invoice_id;
          END IF;

          SELECT i.id,i.period_start,i.period_end,
                 coalesce(a.effective_start,i.period_start) AS covered_start,
                 coalesce(a.effective_end,i.period_end) AS covered_end
            INTO prior_service
          FROM public.platform_saas_invoices i
          LEFT JOIN public.platform_saas_bot_service_activations a
            ON a.invoice_id=i.id
          WHERE i.bot_instance_id=p_bot AND i.owner_user_id=entry.owner_user_id
            AND i.kind='prepayment' AND i.status='issued'
            AND coalesce(a.effective_end,i.period_end)=p_start
          ORDER BY i.period_start DESC LIMIT 1;
          IF prior_service.id IS NOT NULL
             AND public.platform_saas_v2_remaining(prior_service.id)>0
          THEN RAISE EXCEPTION 'Previous bot bill must be fully paid'
            USING ERRCODE='42501'; END IF;
          IF prior_service.id IS NULL AND EXISTS(
            SELECT 1 FROM public.platform_saas_invoices i
            WHERE i.bot_instance_id=p_bot AND i.owner_user_id=entry.owner_user_id
              AND i.kind='prepayment' AND i.status='issued'
          ) THEN
            RAISE EXCEPTION 'Billing anniversary gap requires review'
              USING ERRCODE='42501';
          END IF;
          issue_at:=clock_timestamp();
          SELECT bp.currency_code INTO currency
          FROM public.platform_saas_bot_price_profiles bp
          WHERE bp.bot_instance_id=p_bot
            AND bp.owner_user_id=entry.owner_user_id;
          IF currency IS NULL THEN
            RAISE EXCEPTION 'Uninitialized bot service price: manual review'
              USING ERRCODE='42501';
          END IF;
          IF currency='USD' THEN
            amount_native:=5;
            SELECT rub_per_unit INTO usd_rate
            FROM public.platform_saas_daily_fx_rates
            WHERE currency_code='USD' AND source_name='CBR'
              AND rate_date=(issue_at AT TIME ZONE 'Europe/Moscow')::date;
            IF usd_rate IS NULL OR usd_rate<=0 THEN
              RAISE EXCEPTION 'Verified USD price quote not available'
                USING ERRCODE='42501';
            END IF;
            invoice_rub:=round(5*usd_rate,2);
          ELSE
            amount_native:=150; usd_rate:=1; invoice_rub:=150;
          END IF;

          -- First paid period: service only. Second and subsequent: attach
          -- the previous completed service month's eligible, original-bot
          -- sales to THIS SAME immutable prepayment invoice.
          base_fee:=invoice_rub;
          IF prior_service.id IS NOT NULL THEN
            quote:=public.platform_saas_bot_previous_sales(
              p_bot,prior_service.covered_start,prior_service.covered_end,
              entry.owner_user_id,entry.merchant_id
            );
            SELECT coalesce(sum((row.value->>'charge')::numeric),0)
              INTO combined_fee
            FROM jsonb_array_elements(quote) row;
          END IF;
          invoice_rub:=base_fee+round(combined_fee,2);
          invoice_id := gen_random_uuid();
          INSERT INTO public.platform_saas_invoices(
             id,invoice_key,merchant_id,owner_user_id,kind,
             bot_instance_id,period_start,period_end,due_at,total_rub,
             created_at
          ) VALUES (
             invoice_id,service_key,entry.merchant_id,entry.owner_user_id,
             'prepayment',p_bot,p_start,p_end,p_start,invoice_rub,issue_at
          );
          INSERT INTO public.platform_saas_invoice_lines(
             id,invoice_id,project_id,source_kind,source_id,
             bot_instance_id,original_amount,original_currency,
             rub_per_unit,fee_rate,charge_rub
          ) VALUES (
             gen_random_uuid(),invoice_id,entry.project_id,'bot_fee',p_bot,
             p_bot,amount_native,currency,usd_rate,1,base_fee
          );

          FOR datum IN SELECT value FROM jsonb_array_elements(quote) LOOP
            INSERT INTO public.platform_saas_invoice_lines(
              id,invoice_id,project_id,source_kind,source_id,
              bot_instance_id,original_amount,original_currency,
              rub_per_unit,fee_rate,charge_rub
            ) VALUES (
              gen_random_uuid(),invoice_id,(datum->>'project')::uuid,
              datum->>'kind',(datum->>'id')::uuid,p_bot,
              (datum->>'amount')::numeric,datum->>'currency',
              (datum->>'fx')::numeric,0.05,(datum->>'charge')::numeric
            );
          END LOOP;
          UPDATE public.platform_saas_invoices SET status='issued'
          WHERE id=invoice_id;
          RETURN invoice_id;
        END;
        $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_stars_create_order(
  p_invoice uuid,p_telegram_id bigint,p_central_bot bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; inv record; p record; due numeric;
        fee_count integer; fee_ccy text; fee_native numeric; fee_rub numeric;
        fee_fx numeric;
        fee_usd numeric:=0; commission_rub numeric; fx numeric;
        payment_day date;
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
         max(charge_rub),max(rub_per_unit)
    INTO fee_count,fee_ccy,fee_native,fee_rub,fee_fx
  FROM public.platform_saas_invoice_lines
  WHERE invoice_id=inv.id AND source_kind='bot_fee';
  IF fee_count>1 THEN
    RAISE EXCEPTION 'Ambiguous original service fee' USING ERRCODE='23514';
  ELSIF fee_count=1 THEN
    IF fee_ccy='RUB' AND fee_native=150 AND fee_rub=150 THEN
      -- RUB150 is not replaced with USD5 merely for choosing Stars.
      fee_usd:=0;
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
  -- USD fee and its commission stay priced at the original issued
  -- invoice FX. Changing payment date must NOT reprice a USD invoice.
  IF fee_ccy='USD' AND (fee_fx IS NULL OR fee_fx<=0) THEN
    RAISE EXCEPTION 'Invalid frozen USD service exchange rate'
      USING ERRCODE='23514';
  END IF;
  payment_day:=(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date;
  IF fee_ccy IS DISTINCT FROM 'USD' THEN
    SELECT rub_per_unit INTO fx
    FROM public.platform_saas_daily_fx_rates
    WHERE currency_code='USD' AND source_name='CBR'
      AND rate_date<=payment_day
      AND rate_date>=payment_day-interval '14 days'
      AND source_date<=payment_day
      AND source_date>=payment_day-interval '14 days'
      AND source_date<=rate_date
    ORDER BY rate_date DESC,source_date DESC LIMIT 1;
    IF fx IS NULL OR fx<=0 THEN
      RAISE EXCEPTION 'Verified USD checkout FX unavailable'
        USING ERRCODE='42501';
    END IF;
  END IF;
  IF fee_ccy='USD' THEN
    total_usd:=fee_usd+commission_rub/fee_fx;
  ELSE
    -- This includes old commission-only invoices and full RUB bot bills.
    total_usd:=inv.total_rub/fx;
  END IF;
  IF total_usd<=0 THEN
    RAISE EXCEPTION 'Missing priced invoice lines' USING ERRCODE='23514';
  END IF;
  -- Existing verified receipts and credits shrink ONLY the unpaid portion.
  -- Quote is USD-denominated; remaining RUB is used solely for book credit.
  due_usd:=round(total_usd*due/inv.total_rub,8);
  -- The sales commission was already booked. Apply a distinct 5% Stars
  -- surcharge and the separate payout reserve. 400 Stars is a floor.
  qty:=greatest(400,ceil(
    (due_usd*1.05)/(p.usd_per_star*(1-p.withdrawal_reserve_pct/100))
  ));
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
    usd_quote,usd_per_star,withdrawal_reserve_pct,
    payment_surcharge_pct
  ) VALUES (
    created,p_invoice,actor,p_telegram_id,p_central_bot,
    payload,due,qty::integer,round(due/qty,18),expiry,
    due_usd,p.usd_per_star,p.withdrawal_reserve_pct,5.00
  );
  RETURN jsonb_build_object(
    'order_id',created,'payload',payload,'stars_amount',qty::integer,
    'rub_amount',due::text,'usd_quote',due_usd::text,
    'usd_per_star',p.usd_per_star::text,
    'withdrawal_reserve_pct',p.withdrawal_reserve_pct::text,
    'payment_surcharge_pct','5.00','payment_minimum_stars',400,
    'expires_at',expiry
  );
END; $f$
    """)


def downgrade() -> None:
    raise RuntimeError("Forward only: immutable bot pricing and Stars receipts")
