"""Preserve historic ViMi Pay billing activation intervals (OFF on upgrade).

Revision ID: 0073_saas_windows
Revises: 0072_saas_grants

Old unpaid invoices and deferred commission months survive STOP/ARM. A sale
while billing was paused is not retroactively charged, including a late
captured Telegram Stars or Crypto Pay source.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0073_saas_windows"
down_revision: str | None = "0072_saas_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""CREATE FUNCTION public.platform_saas_first_activation()
RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
  SELECT min(effective_from)
  FROM public.platform_saas_release_audit
  WHERE action='arm' AND effective_from IS NOT NULL;
$f$;
CREATE FUNCTION public.platform_saas_sale_active_at(p_paid timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
  SELECT coalesce((
    SELECT v.action='arm'
    FROM (
      SELECT action,
        CASE WHEN action='arm' THEN effective_from ELSE created_at END
          AS action_at,created_at
      FROM public.platform_saas_release_audit
      WHERE action IN ('arm','stop')
    ) v
    WHERE p_paid IS NOT NULL AND v.action_at<=p_paid
    ORDER BY v.action_at DESC,v.created_at DESC
    LIMIT 1
  ),false);
$f$;""")
    for signature in (
        "platform_saas_first_activation()",
        "platform_saas_sale_active_at(timestamptz)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
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
                  trial_started+interval '72 hours'
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
               OR source.paid_at<greatest(first_charge,trial_started+interval '72 hours')
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
                  trial_started+interval '72 hours'
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
              started,first_trial+interval '72 hours',
              bot.created_at,owner_period_start,
              coalesce(last_issued_end,started)
            );
          END IF;
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
      RETURN jsonb_build_object(
        'enabled',true,'bot_invoices',bot_issued,
        'commission_invoices',commission_issued,'deferred',deferred
      );
    END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_owner_hold_state(
            p_owner uuid, p_at timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          hold_enabled boolean;
          billing_enabled boolean;
          billing_started timestamptz;
          started timestamptz;
          grace integer;
          pending bigint;
          overdue bigint;
        BEGIN
          IF p_owner IS NULL OR p_at IS NULL THEN
            RAISE EXCEPTION 'Invalid hold check'
              USING ERRCODE='22023';
          END IF;
          SELECT enabled,effective_from,grace_days
            INTO hold_enabled,started,grace
            FROM public.platform_saas_hold_control WHERE id=1;
          SELECT enabled, effective_from
            INTO billing_enabled,billing_started
            FROM public.platform_saas_billing_control WHERE id=1;
          IF hold_enabled IS DISTINCT FROM true
             OR billing_enabled IS DISTINCT FROM true
             OR started IS NULL OR billing_started IS NULL
             OR p_at<greatest(started,billing_started) THEN
            RETURN 'disabled';
          END IF;
          -- Preserve old unpaid bills through STOP/ARM without enforcing during pause.
          started:=coalesce(public.platform_saas_first_activation(),started);
          -- Owner/operator accounts are exempt from monetary holds,
          -- even if historic invoices remain in the immutable audit.
          IF EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators po
              ON po.telegram_user_id=u.telegram_user_id
            WHERE u.id=p_owner AND po.role='owner'
          ) THEN RETURN 'clear'; END IF;
          SELECT count(*),count(*) FILTER(
            WHERE greatest(i.due_at,i.issued_at,started)
                  + make_interval(days=>grace)<=p_at
          ) INTO pending,overdue
          FROM public.platform_saas_invoices i
          WHERE i.owner_user_id=p_owner
            AND i.status='issued'
            AND i.issued_at IS NOT NULL
            AND i.issued_at>=started
            AND public.platform_saas_v2_remaining(i.id)>0;
          IF overdue>0 THEN RETURN 'held'; END IF;
          IF pending>0 THEN RETURN 'grace'; END IF;
          RETURN 'clear';
        END;
        $f$
    """)
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
                AND p.paid_at>=trial_started+interval '72 hours'
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
                AND p.paid_at>=trial_start+interval '72 hours'
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
                AND original.paid_at>=trial_started+interval '72 hours'
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


def downgrade() -> None:
    raise RuntimeError("Never discard audited commercial activation windows")
