"""Bot-scoped anniversary billing and no partial bank payments.

Revision ID: 0084_saas_bot_month_billing
Revises: 0083_saas_unified_bill_summary

Forward-only. Existing financial receipts, historical invoices and buyer
entitlements must never be deleted, rebilled, or silently migrated.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0084_saas_bot_month_billing"
down_revision: str | None = "0083_saas_unified_bill_summary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE public.platform_saas_hold_control
      ALTER COLUMN grace_days SET DEFAULT 0
    """)
    op.execute("""
      UPDATE public.platform_saas_hold_control SET grace_days=0 WHERE id=1
    """)
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
      usd_rate_today numeric;
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
      SELECT l.original_amount,l.charge_rub INTO native_usd,base_service_rub
      FROM public.platform_saas_invoice_lines l
      WHERE l.invoice_id=item.id AND l.source_kind='bot_fee'
        AND l.original_currency='USD'
      LIMIT 1;
      IF native_usd IS NULL THEN
        converted:=round(p_requested_rub/rate,2);
      ELSIF p_currency='USD' THEN
        converted:=round(native_usd+(due-base_service_rub)/rate,2);
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
        converted:=round(
          (native_usd*usd_rate_today+due-base_service_rub)/rate,2
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

    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_release_change(
      p_action text, p_expected_bots integer,
      p_expected_other_owners integer
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE
      actor uuid;
      before_state jsonb;
      start_at timestamptz;
      old_billing boolean;
      old_holds boolean;
      enrolled_owner record;
      trial_merchant uuid;
    BEGIN
      actor:=public.platform_saas_require_owner();
      IF p_action NOT IN ('arm','stop','holdon','holdoff') THEN
        RAISE EXCEPTION 'Invalid SaaS pilot action' USING ERRCODE='22023';
      END IF;
      PERFORM pg_advisory_xact_lock(
        hashtextextended('vimi:saas:pilot-control',0)
      );
      before_state:=public.platform_saas_release_status();
      IF p_action IN ('arm','holdon') AND (
           p_expected_bots IS NULL OR p_expected_other_owners IS NULL
           OR p_expected_bots<>(
             before_state->>'registered_bots'
           )::integer
           OR p_expected_other_owners<>(
             before_state->>'nonoperator_owners'
           )::integer
         ) THEN
        RAISE EXCEPTION 'Release bot/account inventory changed, review again'
          USING ERRCODE='23514';
      END IF;
      SELECT enabled INTO old_billing
      FROM public.platform_saas_billing_control WHERE id=1;
      SELECT enabled INTO old_holds
      FROM public.platform_saas_hold_control WHERE id=1;
      IF p_action='arm' THEN
        IF (before_state->>'operator_exemption_pending')::integer>0 THEN
          RAISE EXCEPTION 'Verify platform-owned bot exemptions before rollout'
            USING ERRCODE='42501';
        END IF;
        IF old_billing THEN
          RAISE EXCEPTION 'Commercial pilot already active'
            USING ERRCODE='23514';
        END IF;
        -- No backdated bills, no quota/charge before this DB-clock cutoff.
        start_at:=clock_timestamp()+interval '5 minutes';
        -- Existing connected owners receive remaining/free trial credit,
        -- never retroactive billable sales. Prior claims cannot be reset.
        FOR enrolled_owner IN
          SELECT p.owner_user_id,min(greatest(b.created_at,mp.started_at)) AS first_connected
          FROM public.bot_instances b
          JOIN public.projects p ON p.id=b.project_id
          JOIN public.merchant_project_periods mp
            ON mp.project_id=p.id AND mp.owner_user_id=p.owner_user_id
           AND mp.ended_at IS NULL
          JOIN public.telegram_users u ON u.id=p.owner_user_id
          LEFT JOIN public.platform_operators po
            ON po.telegram_user_id=u.telegram_user_id AND po.role='owner'
          WHERE b.status='registered' AND po.telegram_user_id IS NULL
          GROUP BY p.owner_user_id
        LOOP
          INSERT INTO public.platform_saas_commercial_enrollments(
            owner_user_id,effective_from
          ) VALUES(enrolled_owner.owner_user_id,start_at)
          ON CONFLICT DO NOTHING;
          IF NOT EXISTS(
            SELECT 1 FROM public.merchant_platform_trial_claims t
            WHERE t.owner_user_id=enrolled_owner.owner_user_id
              AND t.policy_key='vimi-saas-72h-v1' AND t.scope='owner'
          ) THEN
            SELECT m.id INTO trial_merchant
            FROM public.merchant_identities m
            WHERE m.initial_owner_user_id=enrolled_owner.owner_user_id
            ORDER BY m.created_at,m.id LIMIT 1;
            IF trial_merchant IS NULL THEN
              trial_merchant:=gen_random_uuid();
              INSERT INTO public.merchant_identities(
                id,initial_owner_user_id
              ) VALUES(trial_merchant,enrolled_owner.owner_user_id);
            END IF;
            INSERT INTO public.merchant_platform_trial_claims(
              id,merchant_id,owner_user_id,policy_key,scope,claimed_at
            ) VALUES(
              gen_random_uuid(),trial_merchant,
              enrolled_owner.owner_user_id,'vimi-saas-72h-v1','owner',
              greatest(enrolled_owner.first_connected,
                       start_at-interval '72 hours')
            ) ON CONFLICT DO NOTHING;
          END IF;
        END LOOP;
        UPDATE public.platform_saas_billing_control
        SET enabled=true,effective_from=start_at WHERE id=1;
        UPDATE public.platform_saas_access_policy
        SET enabled=true,effective_from=start_at WHERE id=1;
        -- Seven-day grace only for newly issued post-rollout invoices.
        UPDATE public.platform_saas_hold_control
        SET enabled=true,effective_from=start_at,grace_days=0 WHERE id=1;
      ELSIF p_action='holdon' THEN
        IF old_billing IS DISTINCT FROM true OR old_holds IS TRUE THEN
          RAISE EXCEPTION 'Activate billing before hold pilot'
            USING ERRCODE='42501';
        END IF;
        start_at:=clock_timestamp()+interval '5 minutes';
        UPDATE public.platform_saas_hold_control
        SET enabled=true,effective_from=start_at,grace_days=0 WHERE id=1;
      ELSIF p_action='holdoff' THEN
        UPDATE public.platform_saas_hold_control
        SET enabled=false,effective_from=NULL WHERE id=1;
      ELSE
        -- Emergency off: no new service charges, trials or holds.
        -- Existing invoice evidence and already bought rights survive.
        UPDATE public.platform_saas_hold_control
        SET enabled=false,effective_from=NULL WHERE id=1;
        UPDATE public.platform_saas_access_policy
        SET enabled=false,effective_from=NULL WHERE id=1;
        UPDATE public.platform_saas_billing_control
        SET enabled=false,effective_from=NULL WHERE id=1;
      END IF;
      INSERT INTO public.platform_saas_release_audit(
        id,actor_id,action,registered_bots,nonoperator_owners,effective_from
      ) VALUES (
        gen_random_uuid(),actor,p_action,
        (before_state->>'registered_bots')::integer,
        (before_state->>'nonoperator_owners')::integer,start_at
      );
      RETURN public.platform_saas_release_status();
    END; $f$
    """)


    op.execute("""
CREATE FUNCTION public.platform_saas_bot_previous_sales(
 p_bot uuid,p_start timestamptz,p_end timestamptz,
 p_owner uuid,p_merchant uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  project uuid;
  source record;
  matched_bot uuid;
  n_bots bigint;
  exemption text;
  rate numeric;
  charged numeric;
  entries jsonb:='[]'::jsonb;
BEGIN
  SELECT b.project_id INTO project FROM public.bot_instances b
  JOIN public.projects p ON p.id=b.project_id
  WHERE b.id=p_bot AND p.owner_user_id=p_owner AND p.merchant_id=p_merchant;
  IF project IS NULL OR p_start IS NULL OR p_end IS NULL OR p_start>=p_end
  THEN RAISE EXCEPTION 'Invalid bot sales interval' USING ERRCODE='42501'; END IF;
  -- Include only verified provider sales. Uncaptured paid provider orders
  -- block issuing an incomplete renewal.
  IF EXISTS(
    SELECT 1 FROM (
      SELECT 'file_stars'::text kind,f.id,f.project_id,f.paid_at
      FROM public.file_sale_purchases f
      WHERE f.status='paid' AND f.telegram_charge_id IS NOT NULL
      UNION ALL
      SELECT 'subscription_stars',o.id,o.project_id,o.paid_at
      FROM public.subscription_star_orders o WHERE o.status='paid'
      UNION ALL
      SELECT 'crypto',ci.id,ci.project_id,ci.paid_at
      FROM public.crypto_pay_invoices ci
      JOIN public.crypto_pay_apps a ON a.id=ci.app_id
       AND a.project_id=ci.project_id AND a.environment='mainnet'
      WHERE ci.status='paid'
    ) raw
    WHERE raw.project_id=project
      AND raw.paid_at>=p_start AND raw.paid_at<p_end
      AND public.platform_saas_sale_active_at(raw.paid_at)
      AND raw.paid_at>=public.platform_saas_billable_after(p_owner)
      AND (
        (raw.kind='crypto' AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_crypto_sales c
          WHERE c.invoice_id=raw.id AND c.owner_user_id=p_owner
        ))
        OR (raw.kind<>'crypto' AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_star_sales s
          WHERE s.source_kind=raw.kind AND s.source_id=raw.id
            AND s.owner_user_id=p_owner
        ))
      )
  ) THEN
    RAISE EXCEPTION 'Uncaptured provider sale prevents billing'
      USING ERRCODE='23514';
  END IF;
  FOR source IN
    SELECT 'subscription'::text AS kind,p.id,p.project_id,
           p.final_amount::numeric AS amount,p.currency_code::text AS currency,
           p.approved_at AS paid_at,NULL::numeric AS provider_fx
    FROM public.payments p
    WHERE p.project_id=project AND p.approved_at>=p_start
      AND p.approved_at<p_end
    UNION ALL
    SELECT 'file_manual',f.id,f.project_id,f.amount::numeric,
           f.currency_code::text,f.paid_at,NULL::numeric
    FROM public.file_sale_manual_orders f
    WHERE f.project_id=project AND f.status='approved'
      AND f.paid_at>=p_start AND f.paid_at<p_end
    UNION ALL
    SELECT s.source_kind,s.source_id,s.project_id,
           s.stars_amount::numeric,'XTR'::text,s.paid_at,
           round(s.usd_per_star*x.rub_per_unit,18)
    FROM public.platform_saas_star_sales s
    LEFT JOIN public.platform_saas_daily_fx_rates x
      ON x.currency_code='USD' AND x.source_name='CBR'
     AND x.rate_date=(s.paid_at AT TIME ZONE 'Europe/Moscow')::date
    WHERE s.billing_eligible AND s.owner_user_id=p_owner
      AND s.merchant_id=p_merchant AND s.project_id=project
      AND s.paid_at>=p_start AND s.paid_at<p_end
    UNION ALL
    SELECT s.source_kind,s.invoice_id,s.project_id,
           s.original_amount,s.currency_code,s.paid_at,
           round(s.usd_per_unit*x.rub_per_unit,18)
    FROM public.platform_saas_crypto_sales s
    LEFT JOIN public.platform_saas_daily_fx_rates x
      ON x.currency_code='USD' AND x.source_name='CBR'
     AND x.rate_date=s.sale_day
    WHERE s.billing_eligible AND s.owner_user_id=p_owner
      AND s.merchant_id=p_merchant AND s.project_id=project
      AND s.paid_at>=p_start AND s.paid_at<p_end
  LOOP
    IF source.paid_at IS NULL
       OR NOT public.platform_saas_sale_active_at(source.paid_at)
       OR source.paid_at<public.platform_saas_billable_after(p_owner)
       OR NOT EXISTS(
         SELECT 1 FROM public.merchant_project_periods mp
         WHERE mp.project_id=source.project_id
           AND mp.merchant_id=p_merchant AND mp.owner_user_id=p_owner
           AND mp.started_at<=source.paid_at
           AND (mp.ended_at IS NULL OR mp.ended_at>source.paid_at)
       ) THEN CONTINUE; END IF;
    SELECT count(*),max(e.source_id::text)::uuid
      INTO n_bots,matched_bot
    FROM public.platform_usage_events e
    WHERE e.project_id=project AND e.event_type='bot_connected'
      AND e.occurred_at<=source.paid_at
      AND NOT EXISTS(
        SELECT 1 FROM public.platform_usage_events gone
        WHERE gone.project_id=e.project_id AND gone.source_id=e.source_id
          AND gone.event_type='bot_removed' AND gone.occurred_at<=source.paid_at
      );
    IF n_bots<>1 THEN
      RAISE EXCEPTION 'Ambiguous bot sale attribution' USING ERRCODE='23514';
    END IF;
    IF matched_bot IS DISTINCT FROM p_bot THEN CONTINUE; END IF;
    exemption:=public.platform_saas_exemption_state(
      project,p_bot,p_merchant,p_owner,source.paid_at);
    IF exemption='exempt' THEN CONTINUE; END IF;
    IF exemption<>'not_exempt' THEN
      RAISE EXCEPTION 'Sale exemption requires review' USING ERRCODE='42501';
    END IF;
    -- Historical commission lines are globally unique: never rebill
    -- sources charged before this upgrade.
    IF EXISTS(
      SELECT 1 FROM public.platform_saas_invoice_lines l
      WHERE l.source_kind=source.kind AND l.source_id=source.id
    ) THEN CONTINUE; END IF;
    IF source.kind IN ('subscription_stars','file_stars',
                       'subscription_crypto','file_crypto') THEN
      rate:=source.provider_fx;
    ELSIF source.currency='RUB' THEN rate:=1;
    ELSE
      SELECT x.rub_per_unit INTO rate
      FROM public.platform_saas_daily_fx_rates x
      WHERE x.currency_code=source.currency AND x.source_name='CBR'
        AND x.rate_date=(source.paid_at AT TIME ZONE 'Europe/Moscow')::date
        AND x.source_date<=x.rate_date
        AND x.source_date>=x.rate_date-interval '14 days';
    END IF;
    IF rate IS NULL OR rate<=0 OR rate>1000000000 THEN
      RAISE EXCEPTION 'Verified source-date FX unavailable'
        USING ERRCODE='22023';
    END IF;
    charged:=CASE
      WHEN source.kind IN ('subscription_stars','file_stars',
                           'subscription_crypto','file_crypto')
      THEN round(source.amount*rate*0.05,18)
      ELSE round(source.amount*rate*0.05,2)
    END;
    IF charged<=0 THEN CONTINUE; END IF;
    entries:=entries || jsonb_build_array(jsonb_build_object(
      'kind',source.kind,'id',source.id,'project',project,
      'amount',source.amount,'currency',source.currency,
      'fx',rate,'charge',charged
    ));
  END LOOP;
  RETURN entries;
END; $f$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
      public.platform_saas_bot_previous_sales(uuid,timestamptz,timestamptz,uuid,uuid)
      FROM PUBLIC
    """)

    op.execute("""
CREATE FUNCTION public.platform_saas_bot_period_end(
  p_bot uuid,p_start timestamptz
) RETURNS timestamptz LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  anchor timestamptz;
  next_month timestamp;
  last_day integer;
  wanted_day integer;
  time_part interval;
BEGIN
  IF p_bot IS NULL OR p_start IS NULL THEN
    RAISE EXCEPTION 'Missing bot billing anniversary'
      USING ERRCODE='22023';
  END IF;
  SELECT min(i.period_start) INTO anchor
  FROM public.platform_saas_invoices i
  JOIN public.bot_instances b ON b.id=i.bot_instance_id
  JOIN public.projects p ON p.id=b.project_id
  WHERE i.bot_instance_id=p_bot AND i.owner_user_id=p.owner_user_id
    AND i.kind='prepayment' AND i.status='issued'
    AND i.period_start<=p_start;
  anchor:=coalesce(anchor,p_start);
  wanted_day:=extract(day FROM (anchor AT TIME ZONE 'Europe/Moscow'))::integer;
  next_month:=date_trunc(
    'month',p_start AT TIME ZONE 'Europe/Moscow'
  )+interval '1 month';
  last_day:=extract(day FROM
    (next_month+interval '1 month'-interval '1 day')::date
  )::integer;
  time_part:=(anchor AT TIME ZONE 'Europe/Moscow')
      -date_trunc('day',anchor AT TIME ZONE 'Europe/Moscow');
  RETURN (next_month+make_interval(
    days=>least(wanted_day,last_day)-1
  )+time_part) AT TIME ZONE 'Europe/Moscow';
END; $f$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
      public.platform_saas_bot_previous_sales(
        uuid,timestamptz,timestamptz,uuid,uuid
      ) FROM PUBLIC
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
      public.platform_saas_bot_period_end(uuid,timestamptz) FROM PUBLIC
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
            WHERE prior.bot_instance_id=p_bot
              AND prior.kind='prepayment' AND prior.status<>'void'
              AND prior.invoice_key<>service_key
              AND tstzrange(prior.period_start,prior.period_end,'[)')
                  && tstzrange(p_start,p_end,'[)')
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

          SELECT i.id,i.period_start,i.period_end INTO prior_service
          FROM public.platform_saas_invoices i
          WHERE i.bot_instance_id=p_bot AND i.owner_user_id=entry.owner_user_id
            AND i.kind='prepayment' AND i.status='issued'
            AND i.period_end=p_start
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
          SELECT coalesce(
            (SELECT currency_code FROM public.platform_saas_price_profiles
             WHERE owner_user_id=entry.owner_user_id),
            'RUB'
          ) INTO currency;
          IF currency='USD' THEN
            amount_native:=3;
            SELECT rub_per_unit INTO usd_rate
            FROM public.platform_saas_daily_fx_rates
            WHERE currency_code='USD' AND source_name='CBR'
              AND rate_date=(issue_at AT TIME ZONE 'Europe/Moscow')::date;
            IF usd_rate IS NULL OR usd_rate<=0 THEN
              RAISE EXCEPTION 'Verified USD price quote not available'
                USING ERRCODE='42501';
            END IF;
            invoice_rub:=round(3*usd_rate,2);
          ELSE
            amount_native:=150; usd_rate:=1; invoice_rub:=150;
          END IF;

          -- First paid period: service only. Second and subsequent: attach
          -- the previous completed service month's eligible, original-bot
          -- sales to THIS SAME immutable prepayment invoice.
          base_fee:=invoice_rub;
          IF prior_service.id IS NOT NULL THEN
            quote:=public.platform_saas_bot_previous_sales(
              p_bot,prior_service.period_start,prior_service.period_end,
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
CREATE OR REPLACE FUNCTION public.platform_saas_line_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          parent record;
          sale record;
          source_bot uuid;
          bot_count bigint;
          effective_at timestamptz;
          exemption_state text;
          usd_rate numeric;
          original_usd_unit numeric;
          bot_fee_usd_fx numeric;
        BEGIN
          IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'Platform invoice lines are immutable'
              USING ERRCODE='42501';
          END IF;
          SELECT * INTO parent FROM public.platform_saas_invoices
            WHERE id=NEW.invoice_id FOR UPDATE;
          IF parent.id IS NULL OR parent.status <> 'draft' THEN
            RAISE EXCEPTION 'Invoice is not editable' USING ERRCODE='42501';
          END IF;
          IF NEW.source_kind = 'bot_fee' THEN
            SELECT rub_per_unit INTO bot_fee_usd_fx
            FROM public.platform_saas_daily_fx_rates
            WHERE currency_code='USD' AND source_name='CBR'
              AND rate_date=(parent.created_at AT TIME ZONE 'Europe/Moscow')::date;
            IF parent.kind <> 'prepayment'
               OR NEW.source_id IS DISTINCT FROM parent.bot_instance_id
               OR NEW.fee_rate <> 1
               OR NOT (
                 (NEW.original_currency='RUB'
                  AND NEW.original_amount=150
                  AND NEW.rub_per_unit=1 AND NEW.charge_rub=150)
                 OR
                 (NEW.original_currency='USD'
                  AND NEW.original_amount=3
                  AND bot_fee_usd_fx IS NOT NULL
                  AND NEW.rub_per_unit=bot_fee_usd_fx
                  AND NEW.charge_rub=round(3*bot_fee_usd_fx,2))
               )
               OR NOT EXISTS (
                 SELECT 1 FROM public.bot_instances b
                 JOIN public.projects p ON p.id=b.project_id
                 WHERE b.id=NEW.source_id
                   AND b.project_id=NEW.project_id
                   AND p.merchant_id=parent.merchant_id
                   AND p.owner_user_id=parent.owner_user_id
               ) THEN
              RAISE EXCEPTION 'Invalid bot prepayment source'
                USING ERRCODE='23514';
            END IF;
            source_bot := NEW.source_id;
            effective_at := parent.period_start;
          ELSE
            IF parent.kind NOT IN ('commission','prepayment')
               OR NEW.fee_rate <> 0.05 THEN
              RAISE EXCEPTION 'Invalid commission policy'
                USING ERRCODE='23514';
            END IF;
            IF NEW.source_kind = 'subscription' THEN
              SELECT project_id,final_amount AS amount,currency_code,
                     approved_at AS paid_at INTO sale
                FROM public.payments WHERE id=NEW.source_id;
            ELSIF NEW.source_kind = 'file_manual' THEN
              SELECT project_id,amount,currency_code,paid_at
                INTO sale FROM public.file_sale_manual_orders
               WHERE id=NEW.source_id AND status='approved';
            ELSIF NEW.source_kind IN ('file_stars','subscription_stars') THEN
              SELECT project_id,stars_amount::numeric AS amount,
                     'XTR'::text AS currency_code,paid_at
                INTO sale FROM public.platform_saas_star_sales
               WHERE source_kind=NEW.source_kind AND source_id=NEW.source_id
                 AND billing_eligible AND merchant_id=parent.merchant_id
                 AND owner_user_id=parent.owner_user_id;
            ELSIF NEW.source_kind IN ('file_crypto','subscription_crypto') THEN
              SELECT project_id,original_amount AS amount,
                     currency_code,paid_at
                INTO sale FROM public.platform_saas_crypto_sales
               WHERE source_kind=NEW.source_kind AND invoice_id=NEW.source_id
                 AND billing_eligible AND merchant_id=parent.merchant_id
                 AND owner_user_id=parent.owner_user_id;
            ELSE
              RAISE EXCEPTION 'Unknown paid source' USING ERRCODE='23514';
            END IF;
            -- Vendor token and paid source were verified at capture.
            -- Re-check original official USD sale-date FX in the database,
            -- never trust invoice caller's synthetic RUB quote.
            IF NEW.source_kind IN ('file_stars','subscription_stars',
                                   'file_crypto','subscription_crypto') THEN
              SELECT rub_per_unit INTO usd_rate
              FROM public.platform_saas_daily_fx_rates
              WHERE currency_code='USD' AND source_name='CBR'
                AND rate_date=(sale.paid_at AT TIME ZONE 'Europe/Moscow')::date;
              IF NEW.source_kind IN ('file_stars','subscription_stars') THEN
                SELECT usd_per_star INTO original_usd_unit
                FROM public.platform_saas_star_sales
                WHERE source_kind=NEW.source_kind AND source_id=NEW.source_id;
              ELSE
                SELECT usd_per_unit INTO original_usd_unit
                FROM public.platform_saas_crypto_sales
                WHERE source_kind=NEW.source_kind AND invoice_id=NEW.source_id;
              END IF;
              IF usd_rate IS NULL OR original_usd_unit IS NULL OR
                 NEW.rub_per_unit IS DISTINCT FROM
                   round(original_usd_unit*usd_rate,18) THEN
                RAISE EXCEPTION 'Unverified Stars/Crypto sale-day conversion'
                  USING ERRCODE='23514';
              END IF;
            END IF;
            -- Source identity is reconstructed from immutable bot-connected
            -- and bot-removed events at the approval time, not the bot that
            -- happens to be connected when month-end billing runs.
            -- Ambiguous histories are not silently assigned a replacement.
            SELECT count(*), max(connected.source_id::text)::uuid
              INTO bot_count,source_bot
              FROM public.platform_usage_events connected
             WHERE connected.project_id=NEW.project_id
               AND connected.event_type='bot_connected'
               AND connected.occurred_at<=sale.paid_at
               AND NOT EXISTS(
                   SELECT 1 FROM public.platform_usage_events removed
                   WHERE removed.project_id=NEW.project_id
                     AND removed.source_id=connected.source_id
                     AND removed.event_type='bot_removed'
                     AND removed.occurred_at<=sale.paid_at
               );
            IF bot_count<>1 THEN
              RAISE EXCEPTION 'Paid source bot history is ambiguous'
                USING ERRCODE='23514';
            END IF;
            IF NEW.source_kind IN ('file_stars','subscription_stars')
               AND NOT EXISTS(
                 SELECT 1 FROM public.platform_saas_star_sales s
                 JOIN public.bot_instances b ON b.id=source_bot
                   AND b.project_id=s.project_id
                   AND b.telegram_bot_id=s.telegram_bot_id
                 WHERE s.source_kind=NEW.source_kind
                   AND s.source_id=NEW.source_id
               ) THEN
              RAISE EXCEPTION 'Stars source does not belong to original bot'
                USING ERRCODE='23514';
            END IF;
            IF NEW.source_kind IN ('file_crypto','subscription_crypto')
               AND NOT EXISTS(
                 SELECT 1 FROM public.platform_saas_crypto_sales s
                 WHERE s.source_kind=NEW.source_kind
                   AND s.invoice_id=NEW.source_id
                   AND s.bot_instance_id=source_bot
               ) THEN
              RAISE EXCEPTION 'Crypto source does not belong to original bot'
                USING ERRCODE='23514';
            END IF;
            effective_at := sale.paid_at;
            IF sale.project_id IS DISTINCT FROM NEW.project_id
               OR sale.amount IS DISTINCT FROM NEW.original_amount
               OR sale.currency_code IS DISTINCT FROM NEW.original_currency
               OR sale.paid_at IS NULL
               OR NOT EXISTS (
                  SELECT 1 FROM public.merchant_project_periods mp
                  WHERE mp.project_id=NEW.project_id
                    AND mp.merchant_id=parent.merchant_id
                    AND mp.owner_user_id=parent.owner_user_id
                    AND mp.started_at<=sale.paid_at
                    AND (mp.ended_at IS NULL OR mp.ended_at>=sale.paid_at)
               )
               OR (
                 parent.kind='commission' AND
                 (sale.paid_at<parent.period_start
                  OR sale.paid_at>=parent.period_end)
               )
               OR (
                 parent.kind='prepayment' AND (
                   source_bot IS DISTINCT FROM parent.bot_instance_id
                   OR NOT EXISTS(
                     SELECT 1 FROM public.platform_saas_invoices prior
                     WHERE prior.bot_instance_id=parent.bot_instance_id
                       AND prior.owner_user_id=parent.owner_user_id
                       AND prior.kind='prepayment' AND prior.status='issued'
                       AND prior.period_end=parent.period_start
                       AND sale.paid_at>=prior.period_start
                       AND sale.paid_at<prior.period_end
                       AND public.platform_saas_v2_remaining(prior.id)=0
                   )
                 )
               )
               OR (
                 NEW.original_currency='RUB'
                 AND NEW.source_kind NOT IN ('file_crypto','subscription_crypto')
                 AND NEW.rub_per_unit <> 1
               )
               OR (
                 NEW.source_kind IN (
                   'file_stars','subscription_stars',
                   'file_crypto','subscription_crypto'
                 ) AND NEW.charge_rub IS DISTINCT FROM
                   round(sale.amount * NEW.rub_per_unit * 0.05, 18)
               )
               OR (
                 NEW.source_kind NOT IN (
                   'file_stars','subscription_stars',
                   'file_crypto','subscription_crypto'
                 ) AND NEW.charge_rub IS DISTINCT FROM
                   round(sale.amount * NEW.rub_per_unit * 0.05, 2)
               )
            THEN
              RAISE EXCEPTION 'Approved purchase / commission snapshot mismatch'
                USING ERRCODE='23514';
            END IF;
          END IF;
          IF NEW.bot_instance_id IS DISTINCT FROM source_bot THEN
            RAISE EXCEPTION 'Historical bot identity mismatch'
              USING ERRCODE='23514';
          END IF;
          SELECT public.platform_saas_exemption_state(
            NEW.project_id, source_bot, parent.merchant_id,
            parent.owner_user_id, effective_at
          ) INTO exemption_state;
          IF exemption_state <> 'not_exempt' THEN
            RAISE EXCEPTION 'Verified platform channel exempt or needs review'
              USING ERRCODE='42501';
          END IF;
          RETURN NEW;
        END;
        $guard$
    """)

    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_issue_due_batch()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE
      started timestamptz;
      now_at timestamptz:=clock_timestamp();
      period_start timestamptz;
      prior record;
      bot record;
      accounting record;
      previous_month date;
      bot_issued integer:=0;
      commission_issued integer:=0;
      deferred integer:=0;
      invoice uuid;
      first_trial timestamptz;
      owner_period_start timestamptz;
      last_issued_end timestamptz;
      progress record;
      cursor_bot_at timestamptz;
      cursor_bot_id uuid;
      cursor_month date;
      cursor_owner_id uuid;
      cursor_merchant_id uuid;
      recovery_owner record;
      recovery_merchant uuid;
    BEGIN
      PERFORM public.platform_saas_require_owner();
      SELECT effective_from INTO started
      FROM public.platform_saas_billing_control
      WHERE id=1 AND enabled;
      IF started IS NULL OR started>now_at THEN
        RETURN jsonb_build_object(
          'enabled',false,'bot_invoices',0,
          'commission_invoices',0,'deferred',0
        );
      END IF;
      IF NOT pg_try_advisory_xact_lock(
        hashtextextended('vimi:saas:due-reconcile',0)
      ) THEN
        RETURN jsonb_build_object('enabled',true,'busy',true);
      END IF;
      SELECT * INTO progress FROM public.platform_saas_due_progress
      WHERE id=1 FOR UPDATE;
      -- During the activation delay, first-bot trial triggers are inactive.
      -- Reconcile those accounts without rewriting any historical claim.
      FOR recovery_owner IN
        SELECT p.owner_user_id,min(greatest(b.created_at,mp.started_at)) AS first_connected
        FROM public.bot_instances b
        JOIN public.projects p ON p.id=b.project_id
        JOIN public.merchant_project_periods mp
          ON mp.project_id=p.id AND mp.owner_user_id=p.owner_user_id
         AND mp.ended_at IS NULL
        JOIN public.telegram_users u ON u.id=p.owner_user_id
        LEFT JOIN public.platform_operators po
          ON po.telegram_user_id=u.telegram_user_id AND po.role='owner'
        WHERE b.status='registered' AND po.telegram_user_id IS NULL
        GROUP BY p.owner_user_id
      LOOP
        INSERT INTO public.platform_saas_commercial_enrollments(
          owner_user_id,effective_from
        ) VALUES(recovery_owner.owner_user_id,started)
        ON CONFLICT DO NOTHING;
        IF NOT EXISTS(
          SELECT 1 FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=recovery_owner.owner_user_id
            AND t.policy_key='vimi-saas-72h-v1' AND t.scope='owner'
        ) THEN
          SELECT m.id INTO recovery_merchant
          FROM public.merchant_identities m
          WHERE m.initial_owner_user_id=recovery_owner.owner_user_id
          ORDER BY m.created_at,m.id LIMIT 1;
          IF recovery_merchant IS NULL THEN
            recovery_merchant:=gen_random_uuid();
            INSERT INTO public.merchant_identities(
              id,initial_owner_user_id
            ) VALUES(recovery_merchant,recovery_owner.owner_user_id);
          END IF;
          INSERT INTO public.merchant_platform_trial_claims(
            id,merchant_id,owner_user_id,policy_key,scope,claimed_at
          ) VALUES(
            gen_random_uuid(),recovery_merchant,
            recovery_owner.owner_user_id,'vimi-saas-72h-v1','owner',
            greatest(recovery_owner.first_connected,started-interval '72 hours')
          ) ON CONFLICT DO NOTHING;
        END IF;
      END LOOP;
      -- Connected LIVE accounts only. The operator's own network is
      -- explicitly excluded. A bot/account can never be invoiced twice
      -- because the canonical issuer locks the bot and invoice_key.
      FOR bot IN
        SELECT b.id,b.created_at,p.owner_user_id,p.id AS project_id
        FROM public.bot_instances b
        JOIN public.projects p ON p.id=b.project_id
        JOIN public.telegram_users u ON u.id=p.owner_user_id
        LEFT JOIN public.platform_operators o
          ON o.telegram_user_id=u.telegram_user_id AND o.role='owner'
        WHERE b.status='registered' AND o.telegram_user_id IS NULL
        ORDER BY
          CASE WHEN progress.last_bot_id IS NOT NULL
            AND (b.created_at,b.id) <=
                (progress.last_bot_connected_at,progress.last_bot_id)
               THEN 1 ELSE 0 END,
          b.created_at,b.id LIMIT 100
      LOOP
        cursor_bot_at:=bot.created_at;
        cursor_bot_id:=bot.id;
        BEGIN
          SELECT min(claimed_at) INTO first_trial
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=bot.owner_user_id AND t.scope='owner'
            AND t.policy_key='vimi-saas-72h-v1';
          IF first_trial IS NULL THEN deferred:=deferred+1;CONTINUE;END IF;
          SELECT mp.started_at INTO owner_period_start
          FROM public.merchant_project_periods mp
          WHERE mp.project_id=bot.project_id
            AND mp.owner_user_id=bot.owner_user_id
            AND mp.ended_at IS NULL;
          IF owner_period_start IS NULL THEN
            deferred:=deferred+1;CONTINUE;
          END IF;
          -- A transferred bot must not inherit the previous owner's debt,
          -- but service periods of both owners must never overlap.
          SELECT max(i.period_end) INTO last_issued_end
          FROM public.platform_saas_invoices i
          WHERE i.bot_instance_id=bot.id AND i.kind='prepayment'
            AND i.status='issued';
          SELECT i.id,i.period_end,i.status
            INTO prior
          FROM public.platform_saas_invoices i
          WHERE i.bot_instance_id=bot.id AND i.kind='prepayment'
            AND i.status='issued'
            AND i.owner_user_id=bot.owner_user_id
            AND i.period_start>=owner_period_start
          ORDER BY i.period_end DESC LIMIT 1;
          IF prior.id IS NOT NULL THEN
            -- Do not stack future debts while the prior month's bill
            -- remains unpaid. Once paid, renew with a real calendar month.
            IF public.platform_saas_v2_remaining(prior.id)>0 THEN
              CONTINUE;
            END IF;
            period_start:=prior.period_end;
          ELSE
            period_start:=greatest(
              started,public.platform_saas_billable_after(bot.owner_user_id),
              bot.created_at,owner_period_start,
              coalesce(last_issued_end,started)
            );
          END IF;
          -- Grandfathered owners may not receive a prepayment before the
          -- full transitional 72-hour window has finished. New owners keep
          -- their existing advance invoice due at the end of their trial.
          IF public.platform_saas_is_transition_owner(bot.owner_user_id)
             AND period_start>now_at THEN CONTINUE; END IF;
          IF period_start>now_at THEN CONTINUE; END IF;
          invoice:=public.platform_saas_issue_bot_invoice(
            bot.id,period_start,
            public.platform_saas_bot_period_end(bot.id,period_start)
          );
          IF invoice IS NOT NULL THEN bot_issued:=bot_issued+1; END IF;
        EXCEPTION WHEN OTHERS THEN
          -- Audit-safe deferred invoice: never issue with uncertain
          -- owner, FX, source or exemption evidence. Retry later.
          deferred:=deferred+1;
        END;
      END LOOP;
      IF cursor_bot_id IS NOT NULL THEN
        UPDATE public.platform_saas_due_progress SET
          last_bot_connected_at=cursor_bot_at,
          last_bot_id=cursor_bot_id,updated_at=clock_timestamp()
        WHERE id=1;
      END IF;
      -- Only renewal invoices contain the per-bot, previous-period 5%.
      -- Do not issue a second calendar-month merchant commission invoice.
      PERFORM public.platform_saas_reconcile_verified_refunds(NULL);
      RETURN jsonb_build_object(
        'enabled',true,'bot_invoices',bot_issued,
        'commission_invoices',commission_issued,'deferred',deferred
      );
    END; $f$
    """)

    op.execute("""
CREATE FUNCTION public.platform_saas_bot_hold_state(
  p_bot uuid,p_at timestamptz
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  owner_id uuid;
  merchant_id uuid;
  project_id uuid;
  started timestamptz;
  billing_started timestamptz;
  enabled boolean;
  is_unpaid boolean;
  last_covered timestamptz;
BEGIN
  IF p_bot IS NULL OR p_at IS NULL THEN
    RAISE EXCEPTION 'Invalid bot hold check' USING ERRCODE='22023';
  END IF;
  SELECT p.owner_user_id,p.merchant_id,b.project_id
    INTO owner_id,merchant_id,project_id
  FROM public.bot_instances b JOIN public.projects p ON p.id=b.project_id
  WHERE b.id=p_bot AND b.status='registered';
  IF owner_id IS NULL THEN RETURN 'disabled'; END IF;
  SELECT h.enabled,h.effective_from INTO enabled,started
  FROM public.platform_saas_hold_control h WHERE h.id=1;
  SELECT c.effective_from INTO billing_started
  FROM public.platform_saas_billing_control c WHERE c.id=1 AND c.enabled;
  IF NOT coalesce(enabled,false) OR started IS NULL
     OR billing_started IS NULL
     OR p_at<greatest(started,billing_started) THEN RETURN 'disabled'; END IF;
  -- Platform staff may administrate someone else's paid channel.
  -- An operator role alone is NOT proof of a creator-owned exemption.
  IF public.platform_saas_exemption_state(
    project_id,p_bot,merchant_id,owner_id,p_at
  )='exempt' THEN RETURN 'clear'; END IF;
  SELECT EXISTS(
    SELECT 1 FROM public.platform_saas_invoices i
    WHERE i.bot_instance_id=p_bot AND i.owner_user_id=owner_id
      AND i.kind='prepayment' AND i.status='issued'
      AND i.due_at<=p_at AND public.platform_saas_v2_remaining(i.id)>0
  ) INTO is_unpaid;
  IF is_unpaid THEN RETURN 'held'; END IF;
  SELECT max(i.period_end) INTO last_covered
  FROM public.platform_saas_invoices i
  WHERE i.bot_instance_id=p_bot AND i.owner_user_id=owner_id
    AND i.kind='prepayment' AND i.status='issued'
    AND public.platform_saas_v2_remaining(i.id)=0;
  IF last_covered IS NOT NULL THEN
    IF last_covered<=p_at THEN RETURN 'held'; END IF;
  ELSIF p_at>=public.platform_saas_billable_after(owner_id) THEN
    -- Trial is over, but billing worker has not issued or confirmed
    -- this bot's initial service invoice yet.
    RETURN 'held';
  END IF;
  RETURN 'clear';
END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_guard_new_commerce()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  scoped uuid;
  source_bot uuid;
  bot_count bigint;
  owner_id uuid;
  merchant_id uuid;
  hold_state text;
  checked_at timestamptz:=clock_timestamp();
BEGIN
  IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
  -- NEW is a trigger RECORD: branch before field access. PostgreSQL
  -- can resolve a missing field even if the other AND term is false.
  IF TG_TABLE_NAME='resources' THEN
    IF NEW.resource_type NOT IN (
      'telegram_channel','telegram_group'
    ) THEN RETURN NEW; END IF;
  END IF;
  scoped:=NULLIF(current_setting('app.current_project_id',true),'')::uuid;
  IF scoped IS DISTINCT FROM NEW.project_id THEN
    RETURN NEW; -- existing tenant RLS fails closed for foreign projects
  END IF;
  -- Another bot's debt can NEVER block connecting an unrelated new bot.
  IF TG_TABLE_NAME='bot_instances' THEN RETURN NEW; END IF;
  SELECT p.owner_user_id,p.merchant_id INTO owner_id,merchant_id
  FROM public.projects p WHERE p.id=NEW.project_id;
  IF owner_id IS NULL THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME IN (
    'file_sale_manual_orders','file_sale_purchases'
  ) THEN
    SELECT b.id INTO source_bot FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id
      AND b.telegram_bot_id=NEW.telegram_bot_id
      AND b.status='registered';
  ELSE
    SELECT count(*),max(b.id::text)::uuid INTO bot_count,source_bot
    FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id AND b.status='registered';
    IF bot_count<>1 THEN RETURN NEW; END IF;
  END IF;
  IF source_bot IS NULL AND TG_TABLE_NAME IN (
    'file_sale_manual_orders','file_sale_purchases'
  ) THEN
    -- Even a malformed pre-insert order cannot bypass the hold by
    -- omitting the bot id. A legitimately issued order remains bound
    -- to the original Telegram bot by its own constraints.
    SELECT count(*),max(b.id::text)::uuid INTO bot_count,source_bot
    FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id AND b.status='registered';
    IF bot_count<>1 THEN source_bot:=NULL; END IF;
  END IF;
  IF source_bot IS NULL THEN RETURN NEW; END IF;
  hold_state:=public.platform_saas_bot_hold_state(source_bot,checked_at);
  IF hold_state<>'held' THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME IN (
    'payment_requests','file_sale_manual_orders','file_sale_purchases'
  ) AND public.platform_saas_exemption_state(
      NEW.project_id,source_bot,merchant_id,owner_id,checked_at
  )='exempt' THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'New ViMi Pay checkout/connection is on payment hold'
    USING ERRCODE='P6711';
END; $f$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
        public.platform_saas_bot_hold_state(uuid,timestamptz)
      FROM PUBLIC
    """)

    # Stars subscription orders have a separate trigger from the other
    # commerce routes: do not accidentally retain the owner-wide hold.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_guard_new_star_subscription()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $guard$
DECLARE
  caller_project uuid;
  liable_owner uuid;
  merchant uuid;
  source_bot uuid;
  checked_at timestamptz:=clock_timestamp();
BEGIN
  caller_project:=NULLIF(current_setting('app.current_project_id',true),'')::uuid;
  IF caller_project IS DISTINCT FROM NEW.project_id THEN RETURN NEW; END IF;
  SELECT p.owner_user_id,p.merchant_id INTO liable_owner,merchant
  FROM public.projects p WHERE p.id=NEW.project_id;
  IF liable_owner IS NULL THEN RETURN NEW; END IF;
  SELECT b.id INTO source_bot FROM public.bot_instances b
  WHERE b.project_id=NEW.project_id
    AND b.telegram_bot_id=NEW.telegram_bot_id
    AND b.status='registered';
  IF source_bot IS NULL THEN RETURN NEW; END IF;
  IF public.platform_saas_bot_hold_state(source_bot,checked_at)<>'held'
  THEN RETURN NEW; END IF;
  IF public.platform_saas_exemption_state(
    NEW.project_id,source_bot,merchant,liable_owner,checked_at
  )='exempt' THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'New ViMi Pay subscription checkout is on hold'
    USING ERRCODE='P6711';
END; $guard$
    """)
    # Crypto Pay issues its invoice on a separate provider rail. Gate new
    # issuance by the exact paid bot, without disrupting old confirmations.
    op.execute("""
CREATE FUNCTION public.platform_saas_guard_new_crypto_checkout()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $guard$
DECLARE
  caller_project uuid;
  liable_owner uuid;
  merchant uuid;
  source_bot uuid;
  checked_at timestamptz:=clock_timestamp();
BEGIN
  caller_project:=NULLIF(current_setting('app.current_project_id',true),'')::uuid;
  IF caller_project IS DISTINCT FROM NEW.project_id THEN RETURN NEW; END IF;
  SELECT p.owner_user_id,p.merchant_id INTO liable_owner,merchant
  FROM public.projects p WHERE p.id=NEW.project_id;
  IF liable_owner IS NULL THEN RETURN NEW; END IF;
  SELECT b.id INTO source_bot FROM public.bot_instances b
  WHERE b.project_id=NEW.project_id
    AND b.telegram_bot_id=NEW.telegram_bot_id
    AND b.status='registered';
  IF source_bot IS NULL THEN RETURN NEW; END IF;
  IF public.platform_saas_bot_hold_state(source_bot,checked_at)<>'held'
  THEN RETURN NEW; END IF;
  IF public.platform_saas_exemption_state(
    NEW.project_id,source_bot,merchant,liable_owner,checked_at
  )='exempt' THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'New ViMi Pay Crypto Pay checkout is on hold'
    USING ERRCODE='P6711';
END; $guard$
    """)
    op.execute("""
      CREATE TRIGGER trg_saas_hold_new_crypto_checkout
      BEFORE INSERT ON public.crypto_pay_invoices
      FOR EACH ROW
      EXECUTE FUNCTION public.platform_saas_guard_new_crypto_checkout()
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
      public.platform_saas_guard_new_crypto_checkout() FROM PUBLIC
    """)

    # Legacy v2 RPC cannot be used to bypass full settlement.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v2_create_attempt(
      p_invoice uuid,p_requested_rub numeric,p_currency text
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
      converted numeric;
      snapped_rub numeric;
      attempt uuid;
      deadline timestamptz;
      native_usd numeric;
      base_service_rub numeric;
      usd_rate_today numeric;
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
      SELECT instructions INTO bank
      FROM public.platform_saas_payment_methods_v2
      WHERE currency_code=p_currency AND enabled;
      -- Legacy RUB details are a fallback only when the owner has never
      -- configured a v2 RUB row. An explicit disabled row always wins.
      IF bank IS NULL AND p_currency='RUB' AND NOT EXISTS(
        SELECT 1 FROM public.platform_saas_payment_methods_v2
        WHERE currency_code='RUB'
      ) THEN
        SELECT instructions INTO bank
        FROM public.platform_saas_payment_details WHERE id=1;
      END IF;
      IF bank IS NULL THEN
        RAISE EXCEPTION 'Platform bank details not published'
          USING ERRCODE='42501';
      END IF;
      -- No obsolete legacy bank message may bypass operator decisions
      -- made in the authoritative named-method catalog. Old callbacks
      -- normally reroute to the new selector, and this DB guard protects
      -- any stale RPC client that still attempts the old flow.
      IF NOT EXISTS(
        SELECT 1 FROM public.platform_saas_payment_catalog_methods m
        JOIN public.platform_saas_payment_catalog_currencies c
          ON c.method_id=m.id AND c.currency_code=p_currency
        WHERE m.enabled AND m.removed_at IS NULL
          AND m.instructions=bank
      ) THEN
        RAISE EXCEPTION 'Legacy bank method no longer available'
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
      SELECT l.original_amount,l.charge_rub INTO native_usd,base_service_rub
      FROM public.platform_saas_invoice_lines l
      WHERE l.invoice_id=item.id AND l.source_kind='bot_fee'
        AND l.original_currency='USD'
      LIMIT 1;
      IF native_usd IS NULL THEN
        converted:=round(p_requested_rub/rate,2);
      ELSIF p_currency='USD' THEN
        converted:=round(native_usd+(due-base_service_rub)/rate,2);
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
        converted:=round(
          (native_usd*usd_rate_today+due-base_service_rub)/rate,2
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
        instructions_snapshot,expires_at
      ) VALUES(
        attempt,item.id,actor,snapped_rub,p_currency,rate,
        converted,day,fx_day,bank,deadline
      );
      RETURN jsonb_build_object(
        'id',attempt,'invoice_id',item.id,'amount_rub',snapped_rub::text,
        'currency',p_currency,'amount',converted::text,
        'rub_per_unit',rate::text,'quote_date',day,
        'source_date',fx_day,'expires_at',deadline,
        'instructions',bank,'remaining_before',due::text
      );
    END; $f$
    """)

    # Real source-linked refunds on mixed bot renewal invoices must
    # remain available to their ORIGINAL bot only, with line-level caps.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_reconcile_verified_refunds(
    p_owner uuid DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $fn$
DECLARE
  actor uuid;
  is_operator boolean;
  entry record;
  claim record;
  bill record;
  already_claimed numeric;
  already_invoiced numeric;
  exact_prior numeric;
  exact_current numeric;
  claim_amount numeric;
  source_status text;
  used_amount numeric;
  available numeric;
  due numeric;
  applied numeric;
  created integer:=0;
  allocated integer:=0;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  SELECT EXISTS (
    SELECT 1 FROM public.telegram_users u
    JOIN public.platform_operators o ON o.telegram_user_id=u.telegram_user_id
    WHERE u.id=actor AND o.role='owner'
  ) INTO is_operator;
  IF actor IS NULL OR (p_owner IS NULL AND NOT is_operator)
    OR (p_owner IS NOT NULL AND actor<>p_owner AND NOT is_operator)
  THEN
    RAISE EXCEPTION 'Verified credits require owning account or platform owner'
      USING ERRCODE='42501';
  END IF;
  -- Monetary account ledger: one source and one allocation are immutable.
  -- Serialize across capture, bank confirmation and overdue balance reads.
  PERFORM pg_advisory_xact_lock(
    hashtextextended('vimi:saas:verified-refunds',0)
  );
  FOR entry IN
    SELECT src.*,l.id AS line_id,l.charge_rub,
           i.id AS original_invoice_id,i.total_rub AS original_invoice_total,
           fx.rub_per_unit
    FROM (
      SELECT 'stars_file'::text AS refund_kind,c.id AS proof_id,
             s.owner_user_id,s.source_kind,s.source_id,
             s.paid_at,c.commission_credit_usd
      FROM public.platform_saas_star_refund_credits c
      JOIN public.platform_saas_star_sales s ON s.id=c.original_sale_id
        AND s.owner_user_id=c.owner_user_id
        AND s.source_kind='file_stars' AND s.billing_eligible
      UNION ALL
      SELECT 'stars_subscription',c.id,s.owner_user_id,
             s.source_kind,s.source_id,s.paid_at,c.commission_credit_usd
      FROM public.platform_saas_star_subscription_credits c
      JOIN public.platform_saas_star_sales s ON s.id=c.original_sale_id
        AND s.owner_user_id=c.owner_user_id
        AND s.source_kind='subscription_stars' AND s.billing_eligible
      UNION ALL
      SELECT 'crypto_transfer',c.id,s.owner_user_id,
             s.source_kind,s.invoice_id,s.paid_at,c.commission_credit_usd
      FROM public.platform_saas_crypto_transfer_credits c
      JOIN public.platform_saas_crypto_sales s ON s.id=c.original_sale_id
        AND s.owner_user_id=c.owner_user_id AND s.billing_eligible
    ) src
    JOIN public.platform_saas_invoice_lines l
      ON l.source_kind=src.source_kind AND l.source_id=src.source_id
      AND l.fee_rate=0.05
    JOIN public.platform_saas_invoices i ON i.id=l.invoice_id
      AND i.owner_user_id=src.owner_user_id
      AND i.kind IN ('commission','prepayment')
      AND i.status='issued'
    JOIN public.platform_saas_daily_fx_rates fx
      ON fx.rate_date=(src.paid_at AT TIME ZONE 'Europe/Moscow')::date
      AND fx.currency_code='USD' AND fx.source_name='CBR'
      AND fx.source_date<=fx.rate_date
      AND fx.source_date>=fx.rate_date-interval '14 days'
    WHERE (p_owner IS NULL OR src.owner_user_id=p_owner)
      AND NOT EXISTS(
        SELECT 1 FROM public.platform_saas_verified_credit_claims c
        WHERE c.source_kind=src.refund_kind AND c.source_credit_id=src.proof_id
      )
    ORDER BY src.paid_at,src.proof_id LIMIT 200
  LOOP
    -- Serialize with the invoice transition guard. An in-flight VOID must
    -- complete first, and the committed status is rechecked under row lock.
    SELECT i.status INTO source_status
    FROM public.platform_saas_invoices i
    WHERE i.id=entry.original_invoice_id FOR UPDATE;
    IF source_status IS DISTINCT FROM 'issued' THEN CONTINUE; END IF;
    SELECT coalesce(sum(credit_rub),0) INTO already_claimed
    FROM public.platform_saas_verified_credit_claims c
    WHERE c.invoice_line_id=entry.line_id;
    SELECT coalesce(sum(c.credit_rub),0),
           coalesce(sum(c.exact_credit_rub),0)
      INTO already_invoiced,exact_prior
    FROM public.platform_saas_verified_credit_claims c
    JOIN public.platform_saas_invoice_lines l
      ON l.id=c.invoice_line_id
    WHERE l.invoice_id=entry.original_invoice_id;
    exact_current:=round(entry.commission_credit_usd*entry.rub_per_unit,18);
    -- Invoice totals are rounded once after summing exact individual sale
    -- commissions. Rounding each refund independently leaves phantom cents,
    -- or overcredits partial returns. Delta of cumulative rounded totals
    -- assigns any remainder to the precise refund that crosses the boundary.
    claim_amount:=greatest(0,least(
      round(exact_prior+exact_current,2)-already_invoiced,
      entry.charge_rub-already_claimed,
      entry.original_invoice_total-already_invoiced
    ));
    INSERT INTO public.platform_saas_verified_credit_claims(
      id,source_kind,source_credit_id,invoice_line_id,owner_user_id,
      credit_rub,exact_credit_rub
    ) VALUES(
      gen_random_uuid(),entry.refund_kind,entry.proof_id,
      entry.line_id,entry.owner_user_id,claim_amount,exact_current
    ) ON CONFLICT(source_kind,source_credit_id) DO NOTHING;
    IF FOUND THEN created:=created+1; END IF;
  END LOOP;
  -- Each source-linked verified refund can be used only up to its face
  -- value. A refund recorded after invoice settlement becomes account
  -- credit for later invoices, not a negative historical payment.
  FOR claim IN
    SELECT c.*, c.credit_rub-coalesce((
       SELECT sum(a.amount_rub)
       FROM public.platform_saas_verified_credit_allocations a
       WHERE a.claim_id=c.id
    ),0) AS available
    FROM public.platform_saas_verified_credit_claims c
    WHERE (p_owner IS NULL OR c.owner_user_id=p_owner)
      AND c.credit_rub>coalesce((
        SELECT sum(a.amount_rub)
        FROM public.platform_saas_verified_credit_allocations a
        WHERE a.claim_id=c.id
      ),0)
      -- Skip claims without an actually payable, unreserved target so a
      -- blocked oldest 200 cannot starve other owners indefinitely.
      AND EXISTS(
        SELECT 1 FROM public.platform_saas_invoices i
        WHERE i.owner_user_id=c.owner_user_id AND i.status='issued'
          AND (
            i.id=(SELECT l.invoice_id FROM public.platform_saas_invoice_lines l
                  WHERE l.id=c.invoice_line_id)
            OR i.bot_instance_id=(SELECT l.bot_instance_id
                FROM public.platform_saas_invoice_lines l
                WHERE l.id=c.invoice_line_id)
          )
          AND public.platform_saas_v2_remaining(i.id)>0
          AND NOT EXISTS(
            SELECT 1 FROM public.platform_saas_verified_credit_allocations a
            WHERE a.claim_id=c.id AND a.invoice_id=i.id
          )
          AND NOT EXISTS(
            SELECT 1 FROM public.platform_saas_payment_attempts_v2 pending
            WHERE pending.invoice_id=i.id
              AND (
                pending.expires_at>clock_timestamp()
                OR EXISTS(
                  SELECT 1 FROM public.platform_saas_payment_proofs_v2 proof
                  WHERE proof.attempt_id=pending.id
                )
              )
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_saas_payment_allocations_v2 ap
                WHERE ap.attempt_id=pending.id
              )
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_saas_payment_rejections_v2 rp
                WHERE rp.attempt_id=pending.id
              )
          )
      )
    ORDER BY c.recorded_at,c.id LIMIT 200
  LOOP
    available:=claim.available;
    FOR bill IN
      SELECT i.id FROM public.platform_saas_invoices i
      WHERE i.owner_user_id=claim.owner_user_id AND i.status='issued'
        -- Preserve original-bot refunds: never use a verified credit
        -- against another bot's invoice from the same merchant account.
        AND (
          i.id=(SELECT l.invoice_id FROM public.platform_saas_invoice_lines l
                WHERE l.id=claim.invoice_line_id)
          OR i.bot_instance_id=(
             SELECT l.bot_instance_id FROM public.platform_saas_invoice_lines l
             WHERE l.id=claim.invoice_line_id)
        )
        -- A quoted or proof-submitted transfer may be in flight. Preserve
        -- its full immutable bank amount for legitimate operator approval.
        -- Unspent verified credit carries into a later invoice instead.
        AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_payment_attempts_v2 pending
          WHERE pending.invoice_id=i.id
            -- A live quote can already have been paid; after its deadline
            -- an unsubmitted quote is no longer a collectible transfer.
            -- A submitted proof remains reserved until settled/rejected.
            AND (
              pending.expires_at>clock_timestamp()
              OR EXISTS(
                SELECT 1 FROM public.platform_saas_payment_proofs_v2 proof
                WHERE proof.attempt_id=pending.id
              )
            )
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_payment_allocations_v2 ap
              WHERE ap.attempt_id=pending.id
            )
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_payment_rejections_v2 rp
              WHERE rp.attempt_id=pending.id
            )
        )
        AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_verified_credit_allocations a
          WHERE a.claim_id=claim.id AND a.invoice_id=i.id
        )
      ORDER BY CASE WHEN i.id=(
        SELECT l.invoice_id FROM public.platform_saas_invoice_lines l
        WHERE l.id=claim.invoice_line_id
      ) THEN 0 ELSE 1 END, i.due_at,i.id
      FOR UPDATE
    LOOP
      EXIT WHEN available<0.01;
      due:=public.platform_saas_v2_remaining(bill.id);
      applied:=least(available,due);
      IF applied>=0.01 THEN
        INSERT INTO public.platform_saas_verified_credit_allocations(
          id,claim_id,invoice_id,amount_rub
        ) VALUES(gen_random_uuid(),claim.id,bill.id,applied);
        available:=available-applied;
        allocated:=allocated+1;
      END IF;
    END LOOP;
  END LOOP;
  RETURN jsonb_build_object('claimed',created,'allocated',allocated);
END; $fn$
    """)

def downgrade() -> None:
    raise RuntimeError(
        "Commercial invoices and verified bank evidence cannot be rolled back"
    )
