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
CREATE FUNCTION public.platform_saas_release_change(
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
  time_part:=(anchor AT TIME ZONE 'Europe/Moscow')::time;
  RETURN (next_month+make_interval(
    days=>least(wanted_day,last_day)-1
  )+time_part) AT TIME ZONE 'Europe/Moscow';
END; $f$
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

def downgrade() -> None:
    raise RuntimeError(
        "Commercial invoices and verified bank evidence cannot be rolled back"
    )
