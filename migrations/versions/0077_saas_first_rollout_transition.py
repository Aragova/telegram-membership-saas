"""One-time live onboarding transition: preserve evidence, fix prepayment.

Revision ID: 0077_saas_transition
Revises: 0076_owner_trial_view

Twenty-three bots existed on two nonoperator test accounts before the first
commercial launch. Their backdated 72h claims produced immediate service
invoices. A genuinely new account's future-due prepayment is valid. Preserve
that account's invoice, and keep the original immutable trial claims.
Billable-after uses an extra one-time 72h transition from first enrollment
for owners with a connected bot at that first rollout, not upon reactivation.
A narrow migration-time reconciliation voids ONLY uncollected pretrial bills;
paid, quoted, proven, credited and settled invoices are left for manual review.
No schedule, checkout, subscription, or buyer access is modified.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077_saas_transition"
down_revision: str | None = "0076_owner_trial_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""CREATE FUNCTION public.platform_saas_is_transition_owner(p_owner uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $fn$
 SELECT EXISTS (
   SELECT 1
   FROM public.platform_saas_commercial_enrollments e
   JOIN public.merchant_project_periods mp
     ON mp.owner_user_id=e.owner_user_id
    AND mp.started_at<=e.effective_from
    AND (mp.ended_at IS NULL OR mp.ended_at>e.effective_from)
   JOIN public.bot_instances b
     ON b.project_id=mp.project_id
    AND b.created_at<e.effective_from
   WHERE e.owner_user_id=p_owner
 );
$fn$;""")
    op.execute("""CREATE FUNCTION public.platform_saas_billable_after(p_owner uuid)
RETURNS timestamptz LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $fn$
DECLARE
  claim_at timestamptz;
  enrolled_at timestamptz;
BEGIN
  SELECT min(t.claimed_at) INTO claim_at
  FROM public.merchant_platform_trial_claims t
  WHERE t.owner_user_id=p_owner AND t.scope='owner'
    AND t.policy_key='vimi-saas-72h-v1';
  IF claim_at IS NULL THEN RETURN NULL; END IF;
  IF public.platform_saas_is_transition_owner(p_owner) THEN
    SELECT e.effective_from INTO enrolled_at
    FROM public.platform_saas_commercial_enrollments e
    WHERE e.owner_user_id=p_owner;
    RETURN greatest(claim_at,enrolled_at)+interval '72 hours';
  END IF;
  RETURN claim_at+interval '72 hours';
END; $fn$;""")
    for signature in (
        "platform_saas_is_transition_owner(uuid)",
        "platform_saas_billable_after(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    # The same effective trial rule must govern all sales, subscriptions,
    # bank prepayments and display. Existing original claims stay immutable.
    # batch
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
            period_start:=greatest(
              prior.period_end,now_at,bot.created_at,owner_period_start,
              coalesce(last_issued_end,now_at)
            );
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
          IF period_start>now_at+interval '3 days' THEN CONTINUE; END IF;
          invoice:=public.platform_saas_issue_bot_invoice(
            bot.id,period_start,period_start+interval '1 month'
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
      -- Closed Moscow calendar month, once per actual historical
      -- merchant+owner. Source completeness and FX are revalidated by
      -- the canonical 0068 issuer, which fails closed rather than
      -- issuing a partial Stars/Crypto invoice.
      previous_month:=date_trunc(
        'month',(now_at AT TIME ZONE 'Europe/Moscow')::timestamp
        -interval '1 month'
      )::date;
      -- Revisit ALL unsettled closed periods since commercial activation,
      -- not just the latest month. Late FX/Stars/Crypto evidence must
      -- eventually generate the missing bill without duplicate keys.
      FOR accounting IN
        SELECT q.merchant_id,q.owner_user_id,q.billing_month
        FROM (
          SELECT DISTINCT mp.merchant_id,mp.owner_user_id,
                 months.m::date AS billing_month
          FROM generate_series(
            date_trunc('month',public.platform_saas_first_activation()
                         AT TIME ZONE 'Europe/Moscow'),
            date_trunc('month',
                       (now_at AT TIME ZONE 'Europe/Moscow')
                       -interval '1 month'),
            interval '1 month'
          ) AS months(m)
          JOIN public.merchant_project_periods mp
            ON mp.started_at <
              ((months.m+interval '1 month') AT TIME ZONE 'Europe/Moscow')
           AND (mp.ended_at IS NULL OR mp.ended_at>=
                (months.m AT TIME ZONE 'Europe/Moscow'))
          JOIN public.telegram_users u ON u.id=mp.owner_user_id
          LEFT JOIN public.platform_operators po
            ON po.telegram_user_id=u.telegram_user_id AND po.role='owner'
          WHERE po.telegram_user_id IS NULL
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_invoices i
              WHERE i.invoice_key='commission:'||mp.merchant_id::text
                ||':'||mp.owner_user_id::text||':'
                ||to_char(months.m,'YYYY-MM-DD')
                AND i.status='issued'
            )
        ) q
        ORDER BY
          CASE WHEN progress.last_commission_month IS NOT NULL
            AND (q.billing_month,q.owner_user_id,q.merchant_id)<=
                (progress.last_commission_month,
                 progress.last_commission_owner_id,
                 progress.last_commission_merchant_id)
               THEN 1 ELSE 0 END,
          q.billing_month,q.owner_user_id,q.merchant_id
        LIMIT 100
      LOOP
        cursor_month:=accounting.billing_month;
        cursor_owner_id:=accounting.owner_user_id;
        cursor_merchant_id:=accounting.merchant_id;
        BEGIN
          invoice:=public.platform_saas_issue_commission_invoice(
            accounting.merchant_id,accounting.owner_user_id,
            accounting.billing_month,'{}'::jsonb
          );
          IF invoice IS NOT NULL THEN
            commission_issued:=commission_issued+1;
          END IF;
        EXCEPTION WHEN OTHERS THEN
          deferred:=deferred+1;
        END;
      END LOOP;
      IF cursor_month IS NOT NULL THEN
        UPDATE public.platform_saas_due_progress SET
          last_commission_month=cursor_month,
          last_commission_owner_id=cursor_owner_id,
          last_commission_merchant_id=cursor_merchant_id,
          updated_at=clock_timestamp() WHERE id=1;
      END IF;
      PERFORM public.platform_saas_reconcile_verified_refunds(NULL);
      RETURN jsonb_build_object(
        'enabled',true,'bot_invoices',bot_issued,
        'commission_invoices',commission_issued,'deferred',deferred
      );
    END; $f$
    """)
    # bot
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
        BEGIN
          first_charge := public.platform_saas_require_rollout();
          IF p_bot IS NULL OR p_start IS NULL OR p_end IS NULL
             OR p_start<first_charge OR p_end<>p_start+interval '1 month'
             OR p_end<=p_start OR p_start>clock_timestamp()+interval '35 days' THEN
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
             p_bot,amount_native,currency,usd_rate,1,invoice_rub
          );
          UPDATE public.platform_saas_invoices SET status='issued'
          WHERE id=invoice_id;
          RETURN invoice_id;
        END;
        $f$
    """)
    # commission
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_issue_commission_invoice(
            p_merchant uuid, p_owner uuid, p_month date, p_fx jsonb
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          first_charge timestamptz;
          trial_started timestamptz;
          period_start timestamptz;
          period_end timestamptz;
          source record;
          original_bot uuid;
          n_bots integer;
          quote jsonb := '[]'::jsonb;
          datum jsonb;
          fx numeric;
          amount_rub numeric;
          total numeric := 0;
          invoice_id uuid;
          previous record;
          v_invoice_key text;
          exemption text;
        BEGIN
          PERFORM public.platform_saas_require_rollout();
           first_charge := public.platform_saas_first_activation();
          IF p_merchant IS NULL OR p_owner IS NULL
             OR p_month IS NULL OR extract(day from p_month)<>1
             OR p_fx IS NULL OR p_fx<>'{}'::jsonb THEN
            RAISE EXCEPTION 'Invalid commission month or FX policy'
              USING ERRCODE='22023';
          END IF;
          period_start := p_month::timestamp AT TIME ZONE 'Europe/Moscow';
          period_end := (p_month::timestamp + interval '1 month')
                        AT TIME ZONE 'Europe/Moscow';
          IF period_end<=first_charge OR period_end>clock_timestamp() THEN
            RAISE EXCEPTION 'Commission month not closed or before rollout'
              USING ERRCODE='42501';
          END IF;
          IF NOT EXISTS(
            SELECT 1 FROM public.merchant_project_periods mp
            WHERE mp.merchant_id=p_merchant AND mp.owner_user_id=p_owner
          ) OR EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=p_owner AND o.role='owner'
          ) THEN
            RAISE EXCEPTION 'Merchant is not billable'
              USING ERRCODE='42501';
          END IF;
          SELECT min(claimed_at) INTO trial_started
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=p_owner
            AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';
          IF trial_started IS NULL THEN
            RAISE EXCEPTION 'Trial claim is not provisioned'
              USING ERRCODE='42501';
          END IF;
          v_invoice_key := 'commission:'||p_merchant::text||':'||
                         p_owner::text||':'||to_char(p_month,'YYYY-MM-DD');
          PERFORM pg_advisory_xact_lock(
            hashtextextended('saas:'||v_invoice_key,0)
          );

          -- Capture workers are bounded and may skip original crypto
          -- provider invoices with missing paid-time USD proof. A monthly
          -- invoice must never seal while any known eligible paid source
          -- is uncaptured, regardless of whether the other rails are ready.
          IF EXISTS (
            SELECT 1 FROM (
              SELECT 'file_stars'::text AS kind,f.id AS source_id,
                     f.project_id,f.paid_at,
                     sc.started_at AS capture_from
              FROM public.file_sale_purchases f
              CROSS JOIN public.platform_saas_star_capture_control sc
              WHERE f.status='paid' AND f.telegram_charge_id IS NOT NULL
              UNION ALL
              SELECT 'subscription_stars',o.id,o.project_id,o.paid_at,
                     sc.started_at
              FROM public.subscription_star_orders o
              CROSS JOIN public.platform_saas_star_capture_control sc
              WHERE o.status='paid'
              UNION ALL
              SELECT 'crypto',i.id,i.project_id,i.paid_at,cc.started_at
              FROM public.crypto_pay_invoices i
              JOIN public.crypto_pay_apps app
                ON app.id=i.app_id AND app.project_id=i.project_id
              CROSS JOIN public.platform_saas_crypto_capture_control cc
              WHERE i.status='paid' AND app.environment='mainnet'
            ) raw
            WHERE raw.paid_at >= period_start AND raw.paid_at < period_end
              AND public.platform_saas_sale_active_at(raw.paid_at)
              AND raw.paid_at >= greatest(
                  raw.capture_from,first_charge,
                  public.platform_saas_billable_after(p_owner)
              )
              AND EXISTS(
                SELECT 1 FROM public.merchant_project_periods hist
                WHERE hist.merchant_id=p_merchant
                  AND hist.owner_user_id=p_owner
                  AND hist.project_id=raw.project_id
                  AND hist.started_at<=raw.paid_at
                  AND (hist.ended_at IS NULL OR hist.ended_at>raw.paid_at)
              )
              AND (
                (raw.kind='crypto' AND NOT EXISTS(
                   SELECT 1 FROM public.platform_saas_crypto_sales v
                   WHERE v.invoice_id=raw.source_id
                     AND v.merchant_id=p_merchant
                     AND v.owner_user_id=p_owner
                ))
                OR (raw.kind<>'crypto' AND NOT EXISTS(
                   SELECT 1 FROM public.platform_saas_star_sales v
                   WHERE v.source_kind=raw.kind
                     AND v.source_id=raw.source_id
                     AND v.merchant_id=p_merchant
                     AND v.owner_user_id=p_owner
                ))
              )
          ) THEN
            RAISE EXCEPTION
              'Uncaptured paid Stars/Crypto sales block monthly invoice'
              USING ERRCODE='23514';
          END IF;
          SELECT i.id,i.status INTO previous
            FROM public.platform_saas_invoices i
            WHERE i.invoice_key=v_invoice_key FOR UPDATE;
          IF previous.id IS NOT NULL THEN
            IF previous.status<>'issued' THEN
              RAISE EXCEPTION 'Existing commission invoice is not issued'
                USING ERRCODE='42501';
            END IF;
            RETURN previous.id;
          END IF;

          FOR source IN
            SELECT 'subscription'::text AS source_kind,p.id,p.project_id,
                   p.final_amount AS amount,p.currency_code,
                   p.approved_at AS paid_at,NULL::numeric AS source_fx
            FROM public.payments p
            UNION ALL
            SELECT 'file_manual'::text,o.id,o.project_id,o.amount,
                   o.currency_code,o.paid_at,NULL::numeric
            FROM public.file_sale_manual_orders o WHERE o.status='approved'
            UNION ALL
            SELECT s.source_kind,s.source_id,s.project_id,
                   s.stars_amount::numeric,'XTR'::text,s.paid_at,
                   round(s.usd_per_star*fx.rub_per_unit,18)
            FROM public.platform_saas_star_sales s
            LEFT JOIN public.platform_saas_daily_fx_rates fx
              ON fx.currency_code='USD' AND fx.source_name='CBR'
             AND fx.rate_date=(s.paid_at AT TIME ZONE 'Europe/Moscow')::date
            WHERE s.billing_eligible AND s.owner_user_id=p_owner
              AND s.merchant_id=p_merchant
            UNION ALL
            SELECT s.source_kind,s.invoice_id,s.project_id,
                   s.original_amount,s.currency_code,s.paid_at,
                   round(s.usd_per_unit*fx.rub_per_unit,18)
            FROM public.platform_saas_crypto_sales s
            LEFT JOIN public.platform_saas_daily_fx_rates fx
              ON fx.currency_code='USD' AND fx.source_name='CBR'
             AND fx.rate_date=s.sale_day
            WHERE s.billing_eligible AND s.owner_user_id=p_owner
              AND s.merchant_id=p_merchant
          LOOP
            IF source.paid_at IS NULL
                OR NOT public.platform_saas_sale_active_at(source.paid_at)
                OR source.paid_at<period_start
               OR source.paid_at>=period_end
               OR source.paid_at<greatest(first_charge,public.platform_saas_billable_after(p_owner))
               OR NOT EXISTS(
                 SELECT 1 FROM public.merchant_project_periods mp
                 WHERE mp.merchant_id=p_merchant
                   AND mp.owner_user_id=p_owner
                   AND mp.project_id=source.project_id
                   AND mp.started_at<=source.paid_at
                   AND (mp.ended_at IS NULL OR mp.ended_at>=source.paid_at)
               ) THEN CONTINUE; END IF;
            SELECT count(*),max(e.source_id::text)::uuid
              INTO n_bots,original_bot
            FROM public.platform_usage_events e
            WHERE e.project_id=source.project_id
              AND e.event_type='bot_connected'
              AND e.occurred_at<=source.paid_at
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_usage_events gone
                WHERE gone.project_id=source.project_id
                  AND gone.event_type='bot_removed'
                  AND gone.source_id=e.source_id
                  AND gone.occurred_at<=source.paid_at
              );
            IF n_bots<>1 THEN
              RAISE EXCEPTION 'Ambiguous historical bot attribution'
                USING ERRCODE='23514';
            END IF;
            exemption := public.platform_saas_exemption_state(
              source.project_id,original_bot,p_merchant,p_owner,source.paid_at);
            IF exemption='exempt' THEN CONTINUE; END IF;
            IF exemption<>'not_exempt' THEN
              RAISE EXCEPTION 'Ambiguous exemption, review before charging'
                USING ERRCODE='42501';
            END IF;
            IF source.source_kind IN (
                  'file_stars','subscription_stars',
                  'file_crypto','subscription_crypto'
               ) THEN
              fx:=source.source_fx;
              IF fx IS NULL OR fx<=0 THEN
                RAISE EXCEPTION 'Missing Stars/Crypto original USD day FX'
                  USING ERRCODE='22023';
              END IF;
            ELSIF source.currency_code='RUB' THEN
              fx := 1;
            ELSE
              -- Each approved sale uses the authoritative historical
              -- Moscow date. A different day may have a different rate.
              SELECT rate.rub_per_unit INTO fx
                FROM public.platform_saas_daily_fx_rates rate
               WHERE rate.rate_date=
                     (source.paid_at AT TIME ZONE 'Europe/Moscow')::date
                 AND rate.currency_code=source.currency_code
                 AND rate.source_name='CBR'
                 AND rate.source_date<=rate.rate_date
                 AND rate.source_date>=rate.rate_date-interval '14 days';
              IF fx IS NULL THEN
                RAISE EXCEPTION 'Missing verified sale-day FX rate for %',
                  source.currency_code USING ERRCODE='22023';
              END IF;
              -- The historical monthly JSON override is still rejected.

            END IF;
            IF fx<=0 OR fx>1000000000 THEN
              RAISE EXCEPTION 'Invalid sale-day conversion rate'
                USING ERRCODE='22023';
            END IF;
            amount_rub := CASE
              WHEN source.source_kind IN (
                'file_stars','subscription_stars',
                'file_crypto','subscription_crypto'
              ) THEN round(source.amount*fx*0.05,18)
              ELSE round(source.amount*fx*0.05,2)
            END;
            IF amount_rub<=0 THEN CONTINUE; END IF;
            total := total+amount_rub;
            quote := quote || jsonb_build_array(jsonb_build_object(
              'source_kind',source.source_kind,'id',source.id,
              'project',source.project_id,'bot',original_bot,
              'amount',source.amount,'currency',source.currency_code,
              'fx',fx,'charge',amount_rub
            ));
          END LOOP;
          IF round(total,2)=0 THEN RETURN NULL; END IF;
          invoice_id:=gen_random_uuid();
          INSERT INTO public.platform_saas_invoices(
             id,invoice_key,merchant_id,owner_user_id,kind,
             period_start,period_end,due_at,total_rub
          ) VALUES (
             invoice_id,v_invoice_key,p_merchant,p_owner,'commission',
             period_start,period_end,period_end+interval '7 days',round(total,2)
          );
          FOR datum IN SELECT value FROM jsonb_array_elements(quote) LOOP
            INSERT INTO public.platform_saas_invoice_lines(
               id,invoice_id,project_id,source_kind,source_id,
               bot_instance_id,original_amount,original_currency,
               rub_per_unit,fee_rate,charge_rub
            ) VALUES (
               gen_random_uuid(),invoice_id,(datum->>'project')::uuid,
               datum->>'source_kind',(datum->>'id')::uuid,
               (datum->>'bot')::uuid,(datum->>'amount')::numeric,
               datum->>'currency',(datum->>'fx')::numeric,
               0.05,(datum->>'charge')::numeric
            );
          END LOOP;

          -- Capture workers are bounded and may skip original crypto
          -- provider invoices with missing paid-time USD proof. A monthly
          -- invoice must never seal while any known eligible paid source
          -- is uncaptured, regardless of whether the other rails are ready.
          IF EXISTS (
            SELECT 1 FROM (
              SELECT 'file_stars'::text AS kind,f.id AS source_id,
                     f.project_id,f.paid_at,
                     sc.started_at AS capture_from
              FROM public.file_sale_purchases f
              CROSS JOIN public.platform_saas_star_capture_control sc
              WHERE f.status='paid' AND f.telegram_charge_id IS NOT NULL
              UNION ALL
              SELECT 'subscription_stars',o.id,o.project_id,o.paid_at,
                     sc.started_at
              FROM public.subscription_star_orders o
              CROSS JOIN public.platform_saas_star_capture_control sc
              WHERE o.status='paid'
              UNION ALL
              SELECT 'crypto',i.id,i.project_id,i.paid_at,cc.started_at
              FROM public.crypto_pay_invoices i
              JOIN public.crypto_pay_apps app
                ON app.id=i.app_id AND app.project_id=i.project_id
              CROSS JOIN public.platform_saas_crypto_capture_control cc
              WHERE i.status='paid' AND app.environment='mainnet'
            ) raw
            WHERE raw.paid_at >= period_start AND raw.paid_at < period_end
              AND public.platform_saas_sale_active_at(raw.paid_at)
              AND raw.paid_at >= greatest(
                  raw.capture_from,first_charge,
                  public.platform_saas_billable_after(p_owner)
              )
              AND EXISTS(
                SELECT 1 FROM public.merchant_project_periods hist
                WHERE hist.merchant_id=p_merchant
                  AND hist.owner_user_id=p_owner
                  AND hist.project_id=raw.project_id
                  AND hist.started_at<=raw.paid_at
                  AND (hist.ended_at IS NULL OR hist.ended_at>raw.paid_at)
              )
              AND (
                (raw.kind='crypto' AND NOT EXISTS(
                   SELECT 1 FROM public.platform_saas_crypto_sales v
                   WHERE v.invoice_id=raw.source_id
                     AND v.merchant_id=p_merchant
                     AND v.owner_user_id=p_owner
                ))
                OR (raw.kind<>'crypto' AND NOT EXISTS(
                   SELECT 1 FROM public.platform_saas_star_sales v
                   WHERE v.source_kind=raw.kind
                     AND v.source_id=raw.source_id
                     AND v.merchant_id=p_merchant
                     AND v.owner_user_id=p_owner
                ))
              )
          ) THEN
            RAISE EXCEPTION
              'Uncaptured paid Stars/Crypto sales block monthly invoice'
              USING ERRCODE='23514';
          END IF;
          UPDATE public.platform_saas_invoices SET status='issued'
          WHERE id=invoice_id;
          RETURN invoice_id;
        END;
        $f$
    """)
    # stars
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_capture_new_file_stars(
            p_limit integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          p record;
          historical record;
          count_periods integer;
          rate_day date;
          rate numeric;
          control record;
          trial_started timestamptz;
          is_operator boolean;
          inserted integer:=0;
          unresolved integer:=0;
          examined integer:=0;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'Invalid Stars capture batch'
              USING ERRCODE='22023';
          END IF;
          SELECT started_at INTO control
          FROM public.platform_saas_star_capture_control WHERE id=1;
          IF control.started_at IS NULL THEN
            RAISE EXCEPTION 'Stars capture cutoff unavailable'
              USING ERRCODE='42501';
          END IF;
          FOR p IN
            SELECT f.id,f.project_id,f.telegram_bot_id,
                   f.telegram_charge_id,f.stars_amount,f.paid_at
            FROM public.file_sale_purchases f
            WHERE f.status='paid' AND f.paid_at>=control.started_at
              AND f.telegram_charge_id IS NOT NULL
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_saas_star_sales e
                WHERE e.source_kind='file_stars' AND e.source_id=f.id
              )
              -- Invalid old owner periods/rates must not indefinitely
              -- occupy the first 200 rows and starve newer paid buyers.
              AND (SELECT count(*)
                   FROM public.merchant_project_periods h
                   WHERE h.project_id=f.project_id
                     AND h.started_at<=f.paid_at
                     AND (h.ended_at IS NULL OR h.ended_at>f.paid_at))=1
              AND EXISTS(
                SELECT 1 FROM public.platform_saas_star_reward_rates q
                WHERE q.effective_on<=
                   (f.paid_at AT TIME ZONE 'Europe/Moscow')::date
              )
            ORDER BY f.paid_at,f.id LIMIT p_limit
          LOOP
            examined:=examined+1;
            SELECT count(*) INTO count_periods
            FROM public.merchant_project_periods h
            WHERE h.project_id=p.project_id
              AND h.started_at<=p.paid_at
              AND (h.ended_at IS NULL OR h.ended_at>p.paid_at);
            IF count_periods<>1 THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            SELECT h.merchant_id,h.owner_user_id INTO historical
            FROM public.merchant_project_periods h
            WHERE h.project_id=p.project_id
              AND h.started_at<=p.paid_at
              AND (h.ended_at IS NULL OR h.ended_at>p.paid_at);
            rate_day:=(p.paid_at AT TIME ZONE 'Europe/Moscow')::date;
            SELECT effective_on,usd_per_star INTO rate_day,rate
            FROM public.platform_saas_star_reward_rates
            WHERE effective_on<=(p.paid_at AT TIME ZONE 'Europe/Moscow')::date
            ORDER BY effective_on DESC LIMIT 1;
            IF rate IS NULL THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            SELECT EXISTS(
                SELECT 1 FROM public.telegram_users u
                JOIN public.platform_operators o
                  ON o.telegram_user_id=u.telegram_user_id
                WHERE u.id=historical.owner_user_id AND o.role='owner'
            ) INTO is_operator;
            SELECT min(t.claimed_at) INTO trial_started
            FROM public.merchant_platform_trial_claims t
            WHERE t.owner_user_id=historical.owner_user_id
              AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';
            INSERT INTO public.platform_saas_star_sales(
                id,source_kind,source_id,project_id,merchant_id,
                owner_user_id,telegram_bot_id,telegram_charge_id,
                stars_amount,usd_per_star,reward_usd,commission_usd,
                rate_effective_on,paid_at,billing_eligible
            ) VALUES (
                gen_random_uuid(),'file_stars',p.id,p.project_id,
                historical.merchant_id,historical.owner_user_id,
                p.telegram_bot_id,p.telegram_charge_id,p.stars_amount,
                rate,p.stars_amount*rate,
                p.stars_amount*rate*0.05,rate_day,p.paid_at,
                NOT is_operator
                AND trial_started IS NOT NULL
                AND p.paid_at>=public.platform_saas_billable_after(historical.owner_user_id)
                AND public.platform_saas_sale_active_at(p.paid_at)
            ) ON CONFLICT (source_kind,source_id) DO NOTHING;
            IF FOUND THEN inserted:=inserted+1; END IF;
          END LOOP;
          -- Expose stalled historical attribution/rate cases separately
          -- without making paid buyer access depend on billing evidence.
          SELECT count(*) INTO unresolved
          FROM public.file_sale_purchases f
          WHERE f.status='paid' AND f.paid_at>=control.started_at
            AND f.telegram_charge_id IS NOT NULL
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_sales e
              WHERE e.source_kind='file_stars' AND e.source_id=f.id
            )
            AND (
              (SELECT count(*) FROM public.merchant_project_periods h
                WHERE h.project_id=f.project_id
                AND h.started_at<=f.paid_at
                AND (h.ended_at IS NULL OR h.ended_at>f.paid_at))<>1
              OR NOT EXISTS (
                SELECT 1 FROM public.platform_saas_star_reward_rates q
                WHERE q.effective_on<=
                  (f.paid_at AT TIME ZONE 'Europe/Moscow')::date
              )
            );
          RETURN jsonb_build_object(
              'examined',examined,'recorded',inserted,
              'unresolved',unresolved,'billing_activated',EXISTS(
                SELECT 1 FROM public.platform_saas_billing_control
                WHERE id=1 AND enabled=true
              )
          );
        END;
        $f$
    """)
    # crypto
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_capture_crypto_sales(
            p_limit integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          original record;
          historical record;
          rate numeric;
          usd_rub numeric;
          asset_rate numeric;
          rate_provenance text;
          sale_date date;
          original_bot uuid;
          bot_count integer;
          found_periods integer;
          trial_started timestamptz;
          is_operator boolean;
          exemption text;
          eligible boolean;
          processed integer:=0;
          inserted integer:=0;
          unresolved integer:=0;
          cutoff timestamptz;
          scan_state record;
          last_seen_at timestamptz;
          last_seen_id uuid;
          max_at timestamptz;
          max_id uuid;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'Invalid Crypto Pay audit batch'
              USING ERRCODE='22023';
          END IF;
          SELECT started_at INTO cutoff
            FROM public.platform_saas_crypto_capture_control WHERE id=1;
          IF cutoff IS NULL THEN
            RAISE EXCEPTION 'Crypto Pay capture cutoff missing'
              USING ERRCODE='42501';
          END IF;
          SELECT last_paid_at,last_invoice_id,max_paid_at,max_invoice_id
            INTO scan_state
          FROM public.platform_saas_crypto_scan_cursor WHERE id=1 FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Crypto Pay audit scan cursor unavailable'
              USING ERRCODE='42501';
          END IF;
          max_at:=scan_state.max_paid_at;
          max_id:=scan_state.max_invoice_id;
          IF max_at IS NULL THEN
            -- Each pass owns a fixed snapshot horizon. New invoices may
            -- appear during or between batches; they belong to NEXT pass.
            SELECT i.paid_at,i.id INTO max_at,max_id
            FROM public.crypto_pay_invoices i
            JOIN public.crypto_pay_apps a
              ON a.id=i.app_id AND a.project_id=i.project_id
            WHERE i.status='paid' AND i.paid_at>=cutoff
              AND a.environment='mainnet'
            ORDER BY i.paid_at DESC,i.id DESC LIMIT 1;
            UPDATE public.platform_saas_crypto_scan_cursor
              SET max_paid_at=max_at,max_invoice_id=max_id WHERE id=1;
          END IF;
          FOR original IN
            SELECT i.id,i.project_id,i.app_id,i.provider_invoice_id,
                   i.telegram_bot_id,i.kind,i.currency_code,
                   i.currency_type,i.amount,i.paid_at
            FROM public.crypto_pay_invoices i
            JOIN public.crypto_pay_apps a ON a.id=i.app_id
                AND a.project_id=i.project_id
            WHERE i.status='paid' AND i.paid_at>=cutoff
              AND a.environment='mainnet'
              AND max_at IS NOT NULL
              AND (i.paid_at,i.id) <= (max_at,max_id)
              AND (scan_state.last_paid_at IS NULL OR
                   (i.paid_at,i.id)>(
                     scan_state.last_paid_at,scan_state.last_invoice_id
                   ))
              AND NOT EXISTS (
                SELECT 1 FROM public.platform_saas_crypto_sales s
                WHERE s.invoice_id=i.id
              )
            ORDER BY i.paid_at,i.id LIMIT p_limit
          LOOP
            processed:=processed+1;
            last_seen_at:=original.paid_at;
            last_seen_id:=original.id;
            sale_date:=(original.paid_at AT TIME ZONE 'Europe/Moscow')::date;
            rate:=NULL;
            rate_provenance:=NULL;
            IF original.currency_type='fiat' THEN
              IF original.currency_code='USD' THEN
                rate:=1; rate_provenance:='fiat_usd';
              ELSE
                SELECT rub_per_unit INTO usd_rub
                  FROM public.platform_saas_daily_fx_rates
                  WHERE rate_date=sale_date AND currency_code='USD'
                    AND source_name='CBR';
                IF original.currency_code='RUB' THEN
                  asset_rate:=1;
                ELSE
                  SELECT rub_per_unit INTO asset_rate
                    FROM public.platform_saas_daily_fx_rates
                   WHERE rate_date=sale_date
                     AND currency_code=original.currency_code
                     AND source_name='CBR';
                END IF;
                IF usd_rub IS NOT NULL AND asset_rate IS NOT NULL
                   AND usd_rub>0 AND asset_rate>0 THEN
                  rate:=asset_rate/usd_rub;
                  rate_provenance:='cbr_sale_day';
                END IF;
              END IF;
            ELSE
              SELECT e.usd_per_asset INTO rate
              FROM public.crypto_pay_paid_usd_rates e
              WHERE e.invoice_id=original.id
                AND e.project_id=original.project_id
                AND e.paid_at=original.paid_at
                AND e.asset_code=original.currency_code;
              rate_provenance:='crypto_pay_paid_usd_rate';
            END IF;
            IF rate IS NULL OR rate<=0 OR rate>1000000000 THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            -- The quote is stored in numeric(38,18): round it FIRST so
            -- both the stored gross and CHECK constraint use precisely
            -- the same immutable exchange-rate snapshot.
            rate:=round(rate,18);
            IF rate<=0 THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            SELECT count(*) INTO found_periods
              FROM public.merchant_project_periods mp
              WHERE mp.project_id=original.project_id
                AND mp.started_at<=original.paid_at
                AND (mp.ended_at IS NULL OR mp.ended_at>original.paid_at);
            IF found_periods<>1 THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            SELECT mp.merchant_id,mp.owner_user_id INTO historical
              FROM public.merchant_project_periods mp
              WHERE mp.project_id=original.project_id
                AND mp.started_at<=original.paid_at
                AND (mp.ended_at IS NULL OR mp.ended_at>original.paid_at);
            SELECT count(*),max(e.source_id::text)::uuid
              INTO bot_count,original_bot
              FROM public.platform_usage_events e
              WHERE e.project_id=original.project_id
                AND e.event_type='bot_connected'
                AND e.occurred_at<=original.paid_at
                AND NOT EXISTS(
                  SELECT 1 FROM public.platform_usage_events gone
                  WHERE gone.project_id=original.project_id
                    AND gone.event_type='bot_removed'
                    AND gone.source_id=e.source_id
                    AND gone.occurred_at<=original.paid_at
                );
            IF bot_count<>1 OR NOT EXISTS(
              SELECT 1 FROM public.bot_instances b
              WHERE b.id=original_bot AND b.project_id=original.project_id
                AND b.telegram_bot_id=original.telegram_bot_id
            ) THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            exemption:=public.platform_saas_exemption_state(
              original.project_id,original_bot,
              historical.merchant_id,historical.owner_user_id,original.paid_at
            );
            IF exemption NOT IN ('exempt','not_exempt') THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            SELECT EXISTS(
                SELECT 1 FROM public.telegram_users u
                JOIN public.platform_operators o
                  ON o.telegram_user_id=u.telegram_user_id
                WHERE u.id=historical.owner_user_id AND o.role='owner'
            ) INTO is_operator;
            SELECT min(t.claimed_at) INTO trial_started
              FROM public.merchant_platform_trial_claims t
              WHERE t.owner_user_id=historical.owner_user_id
                AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';
            eligible:=NOT is_operator
                AND exemption='not_exempt'
                AND trial_started IS NOT NULL
                AND original.paid_at>=public.platform_saas_billable_after(historical.owner_user_id)
                AND public.platform_saas_sale_active_at(original.paid_at);
            INSERT INTO public.platform_saas_crypto_sales(
                id,invoice_id,project_id,app_id,provider_invoice_id,
                merchant_id,owner_user_id,bot_instance_id,telegram_bot_id,
                source_kind,currency_code,currency_type,original_amount,
                usd_per_unit,gross_usd,commission_usd,rate_source,
                sale_day,paid_at,billing_eligible
            ) VALUES (
                gen_random_uuid(),original.id,original.project_id,
                original.app_id,original.provider_invoice_id,
                historical.merchant_id,historical.owner_user_id,
                original_bot,original.telegram_bot_id,
                original.kind||'_crypto',original.currency_code,
                original.currency_type,original.amount,rate,
                round(original.amount*rate,18),
                round(round(original.amount*rate,18)*0.05,18),
                rate_provenance,sale_date,original.paid_at,eligible
            ) ON CONFLICT (invoice_id) DO NOTHING;
            IF FOUND THEN inserted:=inserted+1; END IF;
          END LOOP;
          -- Every pass completes after its fixed high-water mark, EVEN
          -- while new merchant payments keep arriving. A partial scan
          -- resumes from the last item; after exhausting the frozen
          -- horizon, old missing-FX invoices are eligible for retry.
          IF processed=0
             OR (last_seen_at,last_seen_id)>=(max_at,max_id) THEN
            UPDATE public.platform_saas_crypto_scan_cursor
               SET last_paid_at=NULL,last_invoice_id=NULL,
                   max_paid_at=NULL,max_invoice_id=NULL WHERE id=1;
          ELSE
            UPDATE public.platform_saas_crypto_scan_cursor
               SET last_paid_at=last_seen_at,last_invoice_id=last_seen_id
             WHERE id=1;
          END IF;
          RETURN jsonb_build_object(
              'examined',processed,'recorded',inserted,
              'unresolved',unresolved,
              'billing_activated',EXISTS(
                SELECT 1 FROM public.platform_saas_billing_control
                WHERE id=1 AND enabled=true
              ),
              'invoices_issued',false,
              'source','crypto_pay_getInvoices'
          );
        END $f$
    """)
    # trial
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_owner_trial_status()
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE
          actor uuid;
          now_at timestamptz:=transaction_timestamp();
          trial_started timestamptz;
          trial_ends timestamptz;
          effective_at timestamptz;
          enabled boolean;
          registered integer;
          exempt boolean;
          state text;
          seconds_left bigint;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF actor IS NULL THEN
            RAISE EXCEPTION 'Authenticated account required'
              USING ERRCODE='42501';
          END IF;
          SELECT EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators po
              ON po.telegram_user_id=u.telegram_user_id
            WHERE u.id=actor AND po.role='owner'
          ) INTO exempt;
          SELECT count(*) INTO registered
          FROM public.bot_instances b
          JOIN public.projects p ON p.id=b.project_id
          WHERE p.owner_user_id=actor AND b.status='registered';
          IF exempt THEN
            RETURN jsonb_build_object(
              'state','exempt','billing_enabled',false,
              'trial_started',NULL,'trial_ends_at',NULL,
              'remaining_seconds',NULL
            );
          END IF;
          SELECT b.enabled,b.effective_from INTO enabled,effective_at
          FROM public.platform_saas_billing_control b WHERE b.id=1;
          SELECT min(t.claimed_at) INTO trial_started
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=actor AND t.scope='owner'
            AND t.policy_key='vimi-saas-72h-v1';
          trial_ends:=public.platform_saas_billable_after(actor);
          IF registered=0 THEN
            state:='not_connected';
          ELSIF enabled IS DISTINCT FROM true THEN
            state:='disabled';
          ELSIF effective_at IS NULL OR effective_at>now_at THEN
            state:='scheduled';
          ELSIF trial_started IS NULL THEN
            -- The supervisor may still be enrolling a connection made
            -- during the five-minute ARM delay. Never invent an end date.
            state:='pending';
          ELSIF trial_ends>now_at THEN
            state:='trial';
          ELSE
            state:='ended';
          END IF;
          seconds_left:=CASE WHEN trial_ends>now_at
            THEN floor(extract(epoch FROM trial_ends-now_at))::bigint
            ELSE 0 END;
          RETURN jsonb_build_object(
            'state',state,
            'billing_enabled',coalesce(enabled,false),
            'billing_effective_from',effective_at,
            'trial_started',trial_started,
            'trial_ends_at',trial_ends,
            'remaining_seconds',seconds_left
          );
        END; $fn$
    """)
    # Same due threshold in live preview and subscription Stars capture.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_unified_monthly_preview(
        p_owner uuid, p_month date, p_project uuid DEFAULT NULL
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
    DECLARE
      actor uuid;
      operator_allowed boolean;
      beginning timestamptz;
      ending timestamptz;
      launch_at timestamptz;
      trial_at timestamptz;
      summary jsonb;
      manual_count integer;
      stars_count integer;
      crypto_count integer;
      unrated_count integer;
      commission_rub numeric;
      estimated_usd numeric;
      star_credit_usd numeric;
      crypto_credit_usd numeric;
      missing_source_count integer;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      SELECT EXISTS(
        SELECT 1 FROM public.telegram_users u
        JOIN public.platform_operators po
          ON po.telegram_user_id=u.telegram_user_id
        WHERE u.id=actor AND po.role='owner'
      ) INTO operator_allowed;
      IF p_owner IS NULL OR p_month IS NULL
         OR extract(day FROM p_month)<>1
         OR (actor IS DISTINCT FROM p_owner AND NOT operator_allowed)
         OR (p_project IS NOT NULL AND NOT EXISTS(
           SELECT 1 FROM public.merchant_project_periods mp
           WHERE mp.project_id=p_project AND mp.owner_user_id=p_owner
         ))
      THEN
        RAISE EXCEPTION 'Unverified ViMi Pay commercial account/month'
          USING ERRCODE='42501';
      END IF;
      beginning:=p_month::timestamp AT TIME ZONE 'Europe/Moscow';
      ending:=(p_month::timestamp+interval '1 month')
                AT TIME ZONE 'Europe/Moscow';
      SELECT CASE WHEN b.enabled THEN b.effective_from ELSE NULL END
        INTO launch_at FROM public.platform_saas_billing_control b
       WHERE b.id=1;
      SELECT min(t.claimed_at) INTO trial_at
        FROM public.merchant_platform_trial_claims t
        WHERE t.owner_user_id=p_owner AND t.scope='owner'
          AND t.policy_key='vimi-saas-72h-v1';

      WITH manual AS (
        SELECT p.id AS source_id,'subscription'::text AS source_kind,
               p.project_id,p.approved_at AS paid_at,
               p.final_amount AS native_amount,p.currency_code AS currency
          FROM public.payments p
         WHERE p.approved_at>=beginning AND p.approved_at<ending
           AND (p_project IS NULL OR p.project_id=p_project)
        UNION ALL
        SELECT f.id,'file_manual',f.project_id,f.paid_at,
               f.amount,f.currency_code
          FROM public.file_sale_manual_orders f
         WHERE f.status='approved' AND f.paid_at>=beginning
           AND f.paid_at<ending
           AND (p_project IS NULL OR f.project_id=p_project)
      ),
      attributed AS (
        SELECT m.*,
          (SELECT count(*) FROM public.merchant_project_periods h
           WHERE h.project_id=m.project_id AND h.owner_user_id=p_owner
             AND h.started_at<=m.paid_at
             AND (h.ended_at IS NULL OR h.ended_at>m.paid_at)
          ) AS owner_periods,
          (SELECT count(*) FROM public.platform_usage_events e
           WHERE e.project_id=m.project_id
             AND e.event_type='bot_connected'
             AND e.occurred_at<=m.paid_at
             AND NOT EXISTS(
               SELECT 1 FROM public.platform_usage_events gone
               WHERE gone.project_id=m.project_id
                 AND gone.event_type='bot_removed'
                 AND gone.source_id=e.source_id
                 AND gone.occurred_at<=m.paid_at
             )
          ) AS bot_count,
          (SELECT max(e.source_id::text)::uuid
           FROM public.platform_usage_events e
           WHERE e.project_id=m.project_id
             AND e.event_type='bot_connected'
             AND e.occurred_at<=m.paid_at
             AND NOT EXISTS(
               SELECT 1 FROM public.platform_usage_events gone
               WHERE gone.project_id=m.project_id
                 AND gone.event_type='bot_removed'
                 AND gone.source_id=e.source_id
                 AND gone.occurred_at<=m.paid_at
             )
          ) AS original_bot,
          (SELECT min(h.merchant_id::text)::uuid
           FROM public.merchant_project_periods h
           WHERE h.project_id=m.project_id AND h.owner_user_id=p_owner
             AND h.started_at<=m.paid_at
             AND (h.ended_at IS NULL OR h.ended_at>m.paid_at)
          ) AS merchant_id
        FROM manual m
      ),
      all_sales AS (
        SELECT a.source_id,a.source_kind,a.project_id,a.paid_at,
               a.native_amount,a.currency,
               CASE WHEN a.owner_periods<>1 OR a.bot_count<>1
                  OR a.original_bot IS NULL OR a.merchant_id IS NULL
                 THEN false
                 WHEN launch_at IS NULL OR trial_at IS NULL
                   OR NOT public.platform_saas_sale_active_at(a.paid_at)
                   OR a.paid_at<greatest(
                     launch_at,public.platform_saas_billable_after(p_owner)
                   ) THEN false
                 ELSE public.platform_saas_exemption_state(
                   a.project_id,a.original_bot,a.merchant_id,
                   p_owner,a.paid_at
                 )='not_exempt'
               END AS billable,
               CASE WHEN a.currency='RUB' THEN 1::numeric
                    ELSE rate.rub_per_unit END AS unit_rub
          FROM attributed a
          LEFT JOIN public.platform_saas_daily_fx_rates rate
            ON rate.currency_code=a.currency
           AND rate.rate_date=
                 (a.paid_at AT TIME ZONE 'Europe/Moscow')::date
           AND rate.source_name='CBR'
          WHERE a.owner_periods>0
        UNION ALL
        SELECT s.source_id,s.source_kind,s.project_id,s.paid_at,
               s.stars_amount::numeric,'XTR'::text,
               (s.billing_eligible
                AND s.paid_at>=public.platform_saas_billable_after(p_owner)),
               s.usd_per_star*usd.rub_per_unit
        FROM public.platform_saas_star_sales s
        LEFT JOIN public.platform_saas_daily_fx_rates usd
          ON usd.currency_code='USD'
         AND usd.rate_date=
              (s.paid_at AT TIME ZONE 'Europe/Moscow')::date
        WHERE s.owner_user_id=p_owner
          AND s.paid_at>=beginning AND s.paid_at<ending
          AND (p_project IS NULL OR s.project_id=p_project)
        UNION ALL
        SELECT s.invoice_id,s.source_kind,s.project_id,s.paid_at,
               s.original_amount,s.currency_code,
               (s.billing_eligible
                AND s.paid_at>=public.platform_saas_billable_after(p_owner)),
               s.usd_per_unit*usd.rub_per_unit
        FROM public.platform_saas_crypto_sales s
        LEFT JOIN public.platform_saas_daily_fx_rates usd
          ON usd.currency_code='USD'
         AND usd.rate_date=s.sale_day
        WHERE s.owner_user_id=p_owner
          AND s.paid_at>=beginning AND s.paid_at<ending
          AND (p_project IS NULL OR s.project_id=p_project)
      ),
      totals AS (
        SELECT source_kind, count(*) AS sales,
               count(*) FILTER(WHERE billable) AS eligible_sales,
               count(*) FILTER(WHERE billable AND unit_rub IS NULL) AS unrated,
               coalesce(sum(native_amount)
                   FILTER(WHERE billable),0) AS native_gross,
               -- Manual payments have always been rounded per
               -- accepted purchase. Stars/Crypto keep full sale precision,
               -- then round only the monthly aggregated payable total.
               coalesce(sum(
                 CASE WHEN source_kind IN ('subscription','file_manual')
                   THEN round(native_amount*unit_rub*0.05,2)
                   ELSE native_amount*unit_rub*0.05
                 END
               ) FILTER(WHERE billable AND unit_rub IS NOT NULL),0)
                 AS commission_unrounded_rub
        FROM all_sales GROUP BY source_kind
      )
      SELECT
          coalesce(sum(sales) FILTER(
             WHERE source_kind IN ('subscription','file_manual')),0),
          coalesce(sum(sales) FILTER(
             WHERE source_kind IN ('file_stars','subscription_stars')),0),
          coalesce(sum(sales) FILTER(
             WHERE source_kind IN ('file_crypto','subscription_crypto')),0),
          coalesce(sum(unrated),0),
          round(coalesce(sum(commission_unrounded_rub),0),2),
          coalesce(jsonb_agg(jsonb_build_object(
             'kind',source_kind,'sold',sales,'billable',eligible_sales,
             'missing_rate',unrated,'native_gross',native_gross::text,
             'commission_rub',round(commission_unrounded_rub,2)::text
          ) ORDER BY source_kind),'[]'::jsonb)
      INTO manual_count,stars_count,crypto_count,unrated_count,
           commission_rub,summary
      FROM totals;
      SELECT coalesce(sum(c.commission_credit_usd),0) INTO star_credit_usd
      FROM (
        SELECT c.owner_user_id,c.project_id,c.commission_credit_usd,
               c.refunded_at FROM public.platform_saas_star_refund_credits c
        UNION ALL
        SELECT c.owner_user_id,c.project_id,c.commission_credit_usd,
               c.refunded_at
        FROM public.platform_saas_star_subscription_credits c
      ) c
      WHERE c.owner_user_id=p_owner
        AND c.refunded_at>=beginning AND c.refunded_at<ending
        AND (p_project IS NULL OR c.project_id=p_project);
      SELECT coalesce(sum(c.commission_credit_usd),0)
        INTO crypto_credit_usd
      FROM public.platform_saas_crypto_transfer_credits c
      WHERE c.owner_user_id=p_owner
        AND c.completed_at>=beginning AND c.completed_at<ending
        AND (p_project IS NULL OR c.project_id=p_project);
      SELECT count(*) INTO missing_source_count
      FROM public.crypto_pay_invoices i
      JOIN public.crypto_pay_apps app ON app.id=i.app_id
      JOIN public.projects p ON p.id=i.project_id
      WHERE i.status='paid' AND i.paid_at>=beginning
        AND i.paid_at<ending AND p.owner_user_id=p_owner
        AND (p_project IS NULL OR i.project_id=p_project)
        AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_crypto_sales sale
          WHERE sale.invoice_id=i.id
        );
      RETURN jsonb_build_object(
        'owner_user_id',p_owner,'project_id',p_project,
        'month',p_month,'billing_enabled',launch_at IS NOT NULL,
        'rollout_from',launch_at,'preview_only',true,
        'manual_sales',manual_count,'stars_sales',stars_count,
        'crypto_sales',crypto_count,
        'unrated_billable',unrated_count,
        'uncaptured_crypto',missing_source_count,
        'commission_rub',commission_rub::text,
        'rails',summary,'star_refund_credit_usd',star_credit_usd::text,
        'crypto_refund_credit_usd',crypto_credit_usd::text,
        'credits_applied',false,
        'issue_ready',launch_at IS NOT NULL AND unrated_count=0
                       AND missing_source_count=0
      );
    END;
    $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_capture_new_subscription_stars(
          p_limit integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE
          p record;
          history record;
          rate_day date;
          rate numeric;
          trial_start timestamptz;
          started timestamptz;
          exempt boolean;
          recorded integer:=0;
          unresolved integer:=0;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'Invalid subscription Stars capture batch'
              USING ERRCODE='22023';
          END IF;
          SELECT c.started_at INTO started
          FROM public.platform_saas_star_capture_control c WHERE c.id=1;
          IF started IS NULL THEN
            RAISE EXCEPTION 'Missing Stars capture cutoff'
              USING ERRCODE='42501';
          END IF;
          FOR p IN SELECT o.* FROM public.subscription_star_orders o
            WHERE o.status='paid' AND o.paid_at>=started
            AND o.telegram_charge_id IS NOT NULL
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_sales e
              WHERE e.source_kind='subscription_stars'
                AND e.source_id=o.id
            )
            AND (SELECT count(*) FROM public.merchant_project_periods h
                 WHERE h.project_id=o.project_id AND h.started_at<=o.paid_at
                 AND (h.ended_at IS NULL OR h.ended_at>o.paid_at))=1
            AND EXISTS(
              SELECT 1 FROM public.platform_saas_star_reward_rates r
              WHERE r.effective_on<=
                (o.paid_at AT TIME ZONE 'Europe/Moscow')::date
            )
            ORDER BY o.paid_at,o.id LIMIT p_limit
          LOOP
            SELECT merchant_id,owner_user_id INTO history
            FROM public.merchant_project_periods h
            WHERE h.project_id=p.project_id AND h.started_at<=p.paid_at
              AND (h.ended_at IS NULL OR h.ended_at>p.paid_at);
            SELECT effective_on,usd_per_star INTO rate_day,rate
            FROM public.platform_saas_star_reward_rates r
            WHERE r.effective_on<=
              (p.paid_at AT TIME ZONE 'Europe/Moscow')::date
            ORDER BY effective_on DESC LIMIT 1;
            SELECT EXISTS(
              SELECT 1 FROM public.telegram_users u
              JOIN public.platform_operators po
                ON po.telegram_user_id=u.telegram_user_id
              WHERE u.id=history.owner_user_id AND po.role='owner'
            ) INTO exempt;
            SELECT min(t.claimed_at) INTO trial_start
            FROM public.merchant_platform_trial_claims t
            WHERE t.owner_user_id=history.owner_user_id
              AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';
            INSERT INTO public.platform_saas_star_sales(
                id,source_kind,source_id,project_id,merchant_id,
                owner_user_id,telegram_bot_id,telegram_charge_id,
                stars_amount,usd_per_star,reward_usd,commission_usd,
                rate_effective_on,paid_at,billing_eligible
            ) VALUES(
                gen_random_uuid(),'subscription_stars',p.id,p.project_id,
                history.merchant_id,history.owner_user_id,p.telegram_bot_id,
                p.telegram_charge_id,p.stars_amount,rate,
                p.stars_amount*rate,p.stars_amount*rate*0.05,
                rate_day,p.paid_at,
                NOT exempt AND trial_start IS NOT NULL
                AND p.paid_at>=public.platform_saas_billable_after(history.owner_user_id)
                AND public.platform_saas_sale_active_at(p.paid_at)
            ) ON CONFLICT (source_kind,source_id) DO NOTHING;
            IF FOUND THEN recorded:=recorded+1; END IF;
          END LOOP;
          SELECT count(*) INTO unresolved
          FROM public.subscription_star_orders o
          WHERE o.status='paid' AND o.paid_at>=started
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_sales s
              WHERE s.source_kind='subscription_stars' AND s.source_id=o.id
            );
          RETURN jsonb_build_object(
            'recorded',recorded,'unresolved',unresolved,
            'fee_issued',false
          );
        END;
        $fn$
    """)
    # Informational future/due separation; old report keys and payable
    # status remain backward compatible with all existing buyer flows.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_invoice_report(
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
        $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_owner_invoice_report(
            p_page integer, p_size integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE
          actor uuid;
          bank text;
          result jsonb;
        BEGIN
          actor:=NULLIF(
            current_setting('app.current_user_id',true),''
          )::uuid;
          IF actor IS NULL OR NOT EXISTS(
            SELECT 1 FROM public.telegram_users u WHERE u.id=actor
          ) THEN
            RAISE EXCEPTION 'Authenticated owner required'
              USING ERRCODE='42501';
          END IF;
          IF p_page IS NULL OR p_page<0 OR p_page>100000
             OR p_size IS NULL OR p_size<1 OR p_size>8 THEN
            RAISE EXCEPTION 'Invalid statement page' USING ERRCODE='22023';
          END IF;
          -- An explicitly disabled v2 RUB method must never display retired
          -- legacy bank details to the account owner.
          SELECT instructions INTO bank
          FROM public.platform_saas_payment_methods_v2
          WHERE currency_code='RUB' AND enabled;
          IF bank IS NULL AND NOT EXISTS(
            SELECT 1 FROM public.platform_saas_payment_methods_v2
            WHERE currency_code='RUB'
          ) THEN
            SELECT instructions INTO bank
            FROM public.platform_saas_payment_details WHERE id=1;
          END IF;
          SELECT jsonb_build_object(
            'total',(SELECT count(*) FROM public.platform_saas_invoices i
                     WHERE i.owner_user_id=actor AND i.status='issued'),
            'unpaid',(SELECT count(*) FROM public.platform_saas_invoices i
                      WHERE i.owner_user_id=actor AND i.status='issued'
                        AND public.platform_saas_v2_remaining(i.id)>0),
            'outstanding_rub',(
                SELECT coalesce(sum(public.platform_saas_v2_remaining(i.id)),0)::text
                FROM public.platform_saas_invoices i
                WHERE i.owner_user_id=actor AND i.status='issued'
                  AND public.platform_saas_v2_remaining(i.id)>0
            ),
            'due_now_count',(SELECT count(*)
              FROM public.platform_saas_invoices i
              WHERE i.owner_user_id=actor AND i.status='issued'
                AND i.due_at<=clock_timestamp()
                AND public.platform_saas_v2_remaining(i.id)>0),
            'due_now_rub',(SELECT coalesce(
              sum(public.platform_saas_v2_remaining(i.id)),0)::text
              FROM public.platform_saas_invoices i
              WHERE i.owner_user_id=actor AND i.status='issued'
                AND i.due_at<=clock_timestamp()
                AND public.platform_saas_v2_remaining(i.id)>0),
            'upcoming_count',(SELECT count(*)
              FROM public.platform_saas_invoices i
              WHERE i.owner_user_id=actor AND i.status='issued'
                AND i.due_at>clock_timestamp()
                AND public.platform_saas_v2_remaining(i.id)>0),
            'upcoming_rub',(SELECT coalesce(
              sum(public.platform_saas_v2_remaining(i.id)),0)::text
              FROM public.platform_saas_invoices i
              WHERE i.owner_user_id=actor AND i.status='issued'
                AND i.due_at>clock_timestamp()
                AND public.platform_saas_v2_remaining(i.id)>0),
            'details_configured',bank IS NOT NULL,
            'instructions',CASE WHEN EXISTS(
                SELECT 1 FROM public.platform_saas_invoices visible
                WHERE visible.owner_user_id=actor AND visible.status='issued'
            ) THEN bank ELSE NULL END,
            'invoices',coalesce((
              SELECT jsonb_agg(jsonb_build_object(
                  'id',f.id::text,
                  'kind',f.kind,
                  'amount_rub',f.total_rub::text,
                  'period_start',f.period_start,
                  'period_end',f.period_end,
                  'due_at',f.due_at,
                  'issued_at',f.issued_at,
                  'status',CASE WHEN public.platform_saas_v2_remaining(f.id)=0
                                THEN 'paid' ELSE 'unpaid' END,
                  'remaining_rub',public.platform_saas_v2_remaining(f.id)::text
              ) ORDER BY f.due_at DESC,f.id DESC)
              FROM (
                SELECT i.* FROM public.platform_saas_invoices i
                WHERE i.owner_user_id=actor AND i.status='issued'
                ORDER BY i.due_at DESC,i.id DESC
                LIMIT p_size OFFSET p_page*p_size
              ) f
            ),'[]'::jsonb)
          ) INTO result;
          RETURN result;
        END;
        $fn$
    """)
    op.create_table(
        "platform_saas_trial_transition_void_audit",
        sa.Column("invoice_id",sa.Uuid(),
            sa.ForeignKey("platform_saas_invoices.id",ondelete="RESTRICT"),
            primary_key=True),
        sa.Column("owner_user_id",sa.Uuid(),nullable=False),
        sa.Column("old_due_at",sa.DateTime(timezone=True),nullable=False),
        sa.Column("grace_ends_at",sa.DateTime(timezone=True),nullable=False),
        sa.Column("old_total_rub",sa.Numeric(38,2),nullable=False),
        sa.Column("reason",sa.String(80),nullable=False),
        sa.Column("recorded_at",sa.DateTime(timezone=True),nullable=False,
                  server_default=sa.text("clock_timestamp()")),
    )
    op.execute(
        "ALTER TABLE public.platform_saas_trial_transition_void_audit "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_trial_transition_void_audit "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
      CREATE TRIGGER trg_saas_trial_transition_audit_immutable
      BEFORE UPDATE OR DELETE
      ON public.platform_saas_trial_transition_void_audit
      FOR EACH ROW EXECUTE FUNCTION
      public.platform_saas_credit_entry_immutable()
    """)
    # No bot callbacks are added: this privileged DB-only reconciler
    # is run once at upgrade and may be replayed by migration admins.
    # Runtime roles have no EXECUTE grant.
    op.execute("""
      CREATE FUNCTION public.platform_saas_reconcile_first_rollout_invoices()
      RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $repair$
      DECLARE
        item record;
        voided integer:=0;
        skipped integer:=0;
      BEGIN
        PERFORM pg_advisory_xact_lock(
          hashtextextended('vimi:saas:due-reconcile',0)
        );
        FOR item IN
          SELECT i.id,i.owner_user_id,i.due_at,i.total_rub,
                 public.platform_saas_billable_after(i.owner_user_id)
                   AS grace_ends_at
          FROM public.platform_saas_invoices i
          JOIN public.platform_saas_commercial_enrollments e
            ON e.owner_user_id=i.owner_user_id
          WHERE i.kind='prepayment' AND i.status='issued'
            AND i.issued_at>=e.effective_from
            AND i.period_start>=e.effective_from
            AND public.platform_saas_is_transition_owner(i.owner_user_id)
            AND i.period_start<
                public.platform_saas_billable_after(i.owner_user_id)
          ORDER BY i.id
          FOR UPDATE OF i
        LOOP
          -- Nothing already sent to a bank, quoted in another currency,
          -- recorded as a refund or confirmed can be silently voided.
          IF EXISTS(
              SELECT 1 FROM public.platform_saas_receipts r
              WHERE r.invoice_id=item.id
            ) OR EXISTS(
              SELECT 1 FROM public.platform_saas_payment_attempts_v2 a
              WHERE a.invoice_id=item.id
            ) OR EXISTS(
              SELECT 1 FROM public.platform_saas_payment_allocations_v2 a
              WHERE a.invoice_id=item.id
            ) OR EXISTS(
              SELECT 1 FROM public.platform_saas_currency_settlements c
              WHERE c.invoice_id=item.id
            ) OR EXISTS(
              SELECT 1 FROM public.platform_saas_verified_credit_allocations c
              WHERE c.invoice_id=item.id
            ) OR EXISTS(
              SELECT 1 FROM public.platform_saas_verified_credit_claims c
              JOIN public.platform_saas_invoice_lines l
                ON l.id=c.invoice_line_id
              WHERE l.invoice_id=item.id
            ) OR EXISTS(
              SELECT 1 FROM public.platform_saas_refund_credits c
              JOIN public.platform_saas_invoice_lines l
                ON l.id=c.invoice_line_id
              WHERE l.invoice_id=item.id
            )
          THEN
            skipped:=skipped+1;
            CONTINUE;
          END IF;
          UPDATE public.platform_saas_invoices
          SET status='void' WHERE id=item.id AND status='issued';
          IF FOUND THEN
            INSERT INTO public.platform_saas_trial_transition_void_audit(
              invoice_id,owner_user_id,old_due_at,grace_ends_at,
              old_total_rub,reason
            ) VALUES(
              item.id,item.owner_user_id,item.due_at,item.grace_ends_at,
              item.total_rub,'first_rollout_72h_transition'
            ) ON CONFLICT DO NOTHING;
            voided:=voided+1;
          END IF;
        END LOOP;
        RETURN jsonb_build_object('voided',voided,'skipped',skipped);
      END; $repair$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_reconcile_first_rollout_invoices() FROM PUBLIC"
    )
    op.execute(
        "SELECT public.platform_saas_reconcile_first_rollout_invoices()"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Cannot undo an audited commercial trial correction or reissue "
        "voided financial invoices"
    )
