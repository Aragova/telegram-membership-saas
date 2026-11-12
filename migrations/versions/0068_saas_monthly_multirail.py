"""Finish original-source commission invoices across manual, Stars and Crypto.

Revision ID: 0068_saas_multirail
Revises: 0067_saas_partial_payments
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_saas_multirail"
down_revision: str | None = "0067_saas_partial_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Historical source UUIDs stay unique across every merchant/month.
    # The source is still verified by the existing immutable invoice guards.
    op.execute("""
      ALTER TABLE public.platform_saas_invoice_lines
      ALTER COLUMN source_kind TYPE varchar(24)
    """)
    op.execute("""
      ALTER TABLE public.platform_saas_invoice_lines
      DROP CONSTRAINT ck_saas_line_kind
    """)
    op.execute("""
      ALTER TABLE public.platform_saas_invoice_lines
      ADD CONSTRAINT ck_saas_line_kind CHECK(
        source_kind IN (
          'bot_fee','subscription','file_manual',
          'file_stars','subscription_stars',
          'file_crypto','subscription_crypto'
        )
      )
    """)
    # Retain per-sale fee precision. Only the monthly payable total is
    # rounded to kopecks after all original sources have been summed.
    op.alter_column(
        "platform_saas_invoice_lines", "charge_rub",
        existing_type=sa.Numeric(38, 2), type_=sa.Numeric(38, 18),
        existing_nullable=False,
    )
    op.execute("DROP INDEX public.uq_saas_once_per_paid_source")
    op.execute("""
      CREATE UNIQUE INDEX uq_saas_once_per_paid_source
      ON public.platform_saas_invoice_lines(source_kind,source_id)
      WHERE source_kind IN (
        'subscription','file_manual','file_stars',
        'subscription_stars','file_crypto','subscription_crypto'
      )
    """)
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_line_guard()
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
            IF parent.kind <> 'prepayment'
               OR NEW.source_id IS DISTINCT FROM parent.bot_instance_id
               OR NEW.original_currency <> 'RUB'
               OR NEW.original_amount <> 150
               OR NEW.rub_per_unit <> 1
               OR NEW.fee_rate <> 1
               OR NEW.charge_rub <> 150
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
            IF parent.kind <> 'commission' OR NEW.fee_rate <> 0.05 THEN
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
               OR sale.paid_at < parent.period_start
               OR sale.paid_at >= parent.period_end
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
        $guard$""")
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_invoice_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          sum_lines numeric;
          line_count bigint;
        BEGIN
          IF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'SaaS invoices cannot be deleted'
              USING ERRCODE='42501';
          END IF;
          IF TG_OP='INSERT' THEN
            IF NEW.status <> 'draft' OR NEW.issued_at IS NOT NULL
               OR NEW.voided_at IS NOT NULL
               OR NOT EXISTS(
                 SELECT 1 FROM public.merchant_project_periods mp
                 WHERE mp.merchant_id=NEW.merchant_id
                   AND mp.owner_user_id=NEW.owner_user_id
               ) THEN
              RAISE EXCEPTION 'Invalid SaaS invoice origin'
                USING ERRCODE='23514';
            END IF;
            RETURN NEW;
          END IF;
          IF (NEW.id,NEW.invoice_key,NEW.merchant_id,NEW.owner_user_id,
              NEW.kind,NEW.bot_instance_id,NEW.period_start,NEW.period_end,
              NEW.due_at,NEW.total_rub,NEW.created_at)
             IS DISTINCT FROM
             (OLD.id,OLD.invoice_key,OLD.merchant_id,OLD.owner_user_id,
              OLD.kind,OLD.bot_instance_id,OLD.period_start,OLD.period_end,
              OLD.due_at,OLD.total_rub,OLD.created_at) THEN
            RAISE EXCEPTION 'Invoice financial snapshot is immutable'
              USING ERRCODE='42501';
          END IF;
          IF OLD.status='draft' AND NEW.status='issued'
             AND NEW.voided_at IS NULL AND NEW.issued_at IS NULL THEN
            SELECT count(*),COALESCE(sum(charge_rub),0)
            INTO line_count,sum_lines FROM public.platform_saas_invoice_lines
            WHERE invoice_id=OLD.id;
            IF line_count=0 OR round(sum_lines,2)<>OLD.total_rub
               OR EXISTS (
                 SELECT 1 FROM public.platform_saas_invoice_lines l
                 WHERE l.invoice_id=OLD.id AND
                 public.platform_saas_exemption_state(
                   l.project_id, l.bot_instance_id,
                   OLD.merchant_id, OLD.owner_user_id,
                   CASE l.source_kind
                     WHEN 'bot_fee' THEN OLD.period_start
                     WHEN 'subscription' THEN (
                       SELECT p.approved_at FROM public.payments p
                       WHERE p.id=l.source_id
                     )
                     WHEN 'file_manual' THEN (
                       SELECT f.paid_at FROM public.file_sale_manual_orders f
                       WHERE f.id=l.source_id
                     )
                     WHEN 'file_stars' THEN (
                       SELECT s.paid_at FROM public.platform_saas_star_sales s
                       WHERE s.source_kind=l.source_kind AND s.source_id=l.source_id
                     )
                     WHEN 'subscription_stars' THEN (
                       SELECT s.paid_at FROM public.platform_saas_star_sales s
                       WHERE s.source_kind=l.source_kind AND s.source_id=l.source_id
                     )
                     WHEN 'file_crypto' THEN (
                       SELECT s.paid_at FROM public.platform_saas_crypto_sales s
                       WHERE s.source_kind=l.source_kind AND s.invoice_id=l.source_id
                     )
                     WHEN 'subscription_crypto' THEN (
                       SELECT s.paid_at FROM public.platform_saas_crypto_sales s
                       WHERE s.source_kind=l.source_kind AND s.invoice_id=l.source_id
                     )
                   END
                 ) <> 'not_exempt'
               ) THEN
              RAISE EXCEPTION 'Invoice lines / exemption mismatch'
                USING ERRCODE='23514';
            END IF;
            NEW.issued_at := clock_timestamp();
            RETURN NEW;
          END IF;
          IF OLD.status='issued' AND NEW.status='void'
             AND NEW.issued_at IS NOT DISTINCT FROM OLD.issued_at
             AND NEW.voided_at IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM public.platform_saas_receipts
               WHERE invoice_id=OLD.id
             ) THEN
            NEW.voided_at := clock_timestamp();
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'Invalid SaaS invoice state change'
            USING ERRCODE='42501';
        END;
        $guard$""")
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_issue_commission_invoice(
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
          first_charge := public.platform_saas_require_rollout();
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
            IF source.paid_at IS NULL OR source.paid_at<period_start
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
        $f$""")


def downgrade() -> None:
    raise RuntimeError(
        "Refuse rollback of source-linked monthly financial records"
    )
