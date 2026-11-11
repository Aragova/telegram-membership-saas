"""Daily official sale-date FX for ViMi Pay's inactive commercial billing.

Revision ID: 0052_sale_day_fx
Revises: 0051_saas_fx_policy

Keeps historical monthly FX rows immutable; no billing/trial/hold activation.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_sale_day_fx"
down_revision: str | None = "0051_saas_fx_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Immutable original CBR quote at each Moscow sale date. Month-locked
    # snapshots remain for historical audit only, never silently rewritten.
    op.create_table(
        "platform_saas_daily_fx_rates",
        sa.Column("rate_date", sa.Date(), primary_key=True),
        sa.Column("currency_code", sa.String(16), primary_key=True),
        sa.Column("rub_per_unit", sa.Numeric(38, 18), nullable=False),
        sa.Column("source_name", sa.String(40), nullable=False),
        sa.Column("source_date", sa.Date(), nullable=False),
        sa.Column(
            "published_by", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "published_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.CheckConstraint(
            "currency_code IN ('USD','EUR','UAH','BYN')"
            " AND rub_per_unit>0 AND rub_per_unit<=1000000000",
            name="ck_daily_fx_fiat_rate",
        ),
        sa.CheckConstraint(
            "source_name='CBR' AND source_date<=rate_date"
            " AND source_date>=rate_date-interval '14 days'",
            name="ck_daily_fx_source",
        ),
    )
    op.execute(
        "ALTER TABLE public.platform_saas_daily_fx_rates "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_daily_fx_rates "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
        DO $permissions$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            REVOKE ALL ON public.platform_saas_daily_fx_rates
              FROM membership_runtime;
          END IF;
        END $permissions$
    """)
    # Reuse already-deployed immutable trigger function from migration 0051.
    op.execute("""
        CREATE TRIGGER trg_saas_daily_fx_immutable
        BEFORE UPDATE OR DELETE ON public.platform_saas_daily_fx_rates
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_fx_immutable()
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_publish_cbr_daily_fx(
            p_day date, p_currency text, p_rate numeric,
            p_source_day date
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $daily$
        DECLARE
          actor uuid;
          existing numeric;
        BEGIN
          actor:=public.platform_saas_require_owner();
          IF p_day IS NULL OR p_currency NOT IN ('USD','EUR','UAH','BYN')
             OR p_rate IS NULL OR p_rate<=0 OR p_rate>1000000000
             OR p_rate<>round(p_rate,18)
             OR p_source_day IS NULL OR p_source_day>p_day
             OR p_source_day<p_day-interval '14 days' THEN
            RAISE EXCEPTION 'Invalid official daily FX rate'
              USING ERRCODE='22023';
          END IF;
          PERFORM pg_advisory_xact_lock(hashtextextended(
            'vimi-daily-fx:'||p_day::text||':'||p_currency,0
          ));
          SELECT rub_per_unit INTO existing
          FROM public.platform_saas_daily_fx_rates
          WHERE rate_date=p_day AND currency_code=p_currency;
          IF FOUND THEN
            IF existing<>p_rate THEN
              RAISE EXCEPTION 'Published sale-day FX cannot be changed'
                USING ERRCODE='23505';
            END IF;
          ELSE
            INSERT INTO public.platform_saas_daily_fx_rates(
              rate_date,currency_code,rub_per_unit,
              source_name,source_date,published_by
            ) VALUES(p_day,p_currency,p_rate,'CBR',p_source_day,actor);
          END IF;
          RETURN jsonb_build_object(
            'day',p_day,'currency',p_currency,'rub_per_unit',p_rate,
            'source','CBR','source_date',p_source_day
          );
        END;
        $daily$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_daily_fx(p_day date)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $daily$
        DECLARE rates jsonb;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_day IS NULL THEN
            RAISE EXCEPTION 'Invalid daily FX date' USING ERRCODE='22023';
          END IF;
          SELECT coalesce(jsonb_object_agg(
            currency_code,jsonb_build_object(
              'rate',rub_per_unit,'source_date',source_date
            ) ORDER BY currency_code
          ),'{}'::jsonb) INTO rates
          FROM public.platform_saas_daily_fx_rates
          WHERE rate_date=p_day;
          RETURN jsonb_build_object('day',p_day,'rates',rates);
        END;
        $daily$
    """)
    for signature in (
        "platform_saas_publish_cbr_daily_fx(date,text,numeric,date)",
        "platform_saas_daily_fx(date)",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC"
        )
    op.execute("""
        DO $grant$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles
                    WHERE rolname='membership_runtime') THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_publish_cbr_daily_fx(
                date,text,numeric,date
              ), public.platform_saas_daily_fx(date)
              TO membership_runtime;
          END IF;
        END $grant$
    """)

    # Keep the historical owner period, trial, checkout and security
    # decisions: only sale-day FX and the owner-facing quote change.
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
                   p.approved_at AS paid_at
            FROM public.payments p
            UNION ALL
            SELECT 'file_manual'::text,o.id,o.project_id,o.amount,
                   o.currency_code,o.paid_at
            FROM public.file_sale_manual_orders o
            WHERE o.status='approved'
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
            IF source.currency_code='RUB' THEN
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
            amount_rub := round(source.amount*fx*0.05,2);
            IF amount_rub<=0 THEN CONTINUE; END IF;
            total := total+amount_rub;
            quote := quote || jsonb_build_array(jsonb_build_object(
              'source_kind',source.source_kind,'id',source.id,
              'project',source.project_id,'bot',original_bot,
              'amount',source.amount,'currency',source.currency_code,
              'fx',fx,'charge',amount_rub
            ));
          END LOOP;
          IF total=0 THEN RETURN NULL; END IF;
          invoice_id:=gen_random_uuid();
          INSERT INTO public.platform_saas_invoices(
             id,invoice_key,merchant_id,owner_user_id,kind,
             period_start,period_end,due_at,total_rub
          ) VALUES (
             invoice_id,v_invoice_key,p_merchant,p_owner,'commission',
             period_start,period_end,period_end+interval '7 days',total
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
          UPDATE public.platform_saas_invoices SET status='issued'
          WHERE id=invoice_id;
          RETURN invoice_id;
        END;
        $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_project_preview(
            p_project uuid, p_month date
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $preview$
        DECLARE
          actor uuid;
          owner_id uuid;
          own_platform boolean;
          rows jsonb;
          fiat_fee numeric;
          stars_fee numeric;
          unrated_count integer;
          star_count integer;
          today_rates jsonb;
          billing_on boolean;
          first_charge timestamptz;
          trial_started timestamptz;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          SELECT owner_user_id INTO owner_id FROM public.projects
            WHERE id=p_project;
          IF actor IS NULL OR owner_id IS DISTINCT FROM actor THEN
            RAISE EXCEPTION 'Project owner only'
              USING ERRCODE='42501';
          END IF;
          IF p_month IS NULL OR extract(day from p_month)<>1 THEN
            RAISE EXCEPTION 'Invalid month' USING ERRCODE='22023';
          END IF;
          SELECT EXISTS (
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators po
              ON po.telegram_user_id=u.telegram_user_id
            WHERE u.id=owner_id AND po.role='owner'
          ) INTO own_platform;
          SELECT enabled,effective_from INTO billing_on,first_charge
            FROM public.platform_saas_billing_control WHERE id=1;
          SELECT min(t.claimed_at) INTO trial_started
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=owner_id
            AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';

          WITH sales AS (
            SELECT p.final_amount AS amount,p.currency_code AS currency,
                   p.approved_at AS paid_at, 'subscription'::text AS kind
              FROM public.payments p WHERE p.project_id=p_project
            UNION ALL
            SELECT o.amount,o.currency_code,o.paid_at,'file_manual'::text
              FROM public.file_sale_manual_orders o
             WHERE o.project_id=p_project AND o.status='approved'
            UNION ALL
            SELECT f.stars_amount::numeric,'XTR'::text,f.paid_at,'stars'::text
              FROM public.file_sale_purchases f
             WHERE f.project_id=p_project AND f.status='paid'
               AND f.telegram_charge_id IS NOT NULL
          ),
          rated AS (
            SELECT s.amount,s.currency,s.kind,
                   CASE WHEN s.currency='RUB' THEN 1::numeric
                        ELSE fx.rub_per_unit END AS sale_day_rate,
                   (NOT coalesce(billing_on,false)
                    OR (first_charge IS NOT NULL
                        AND trial_started IS NOT NULL
                        AND s.paid_at>=greatest(
                          first_charge,trial_started+interval '72 hours'
                        ))) AS commission_eligible
            FROM sales s
            LEFT JOIN public.platform_saas_daily_fx_rates fx
              ON fx.rate_date=(s.paid_at AT TIME ZONE 'Europe/Moscow')::date
             AND fx.currency_code=s.currency
             AND fx.source_name='CBR'
             AND fx.source_date<=fx.rate_date
             AND fx.source_date>=fx.rate_date-interval '14 days'
            WHERE s.paid_at >= (p_month::timestamp AT TIME ZONE 'Europe/Moscow')
              AND s.paid_at < ((p_month::timestamp + interval '1 month')
                               AT TIME ZONE 'Europe/Moscow')
              AND EXISTS (
                SELECT 1 FROM public.merchant_project_periods mp
                WHERE mp.project_id=p_project
                  AND mp.owner_user_id=actor
                  AND mp.started_at<=s.paid_at
                  AND (mp.ended_at IS NULL OR mp.ended_at>s.paid_at)
              )
          ),
          aggregates AS (
            SELECT currency,count(*) AS operations,
                   sum(amount) AS gross,
                   count(*) FILTER (
                     WHERE sale_day_rate IS NULL AND commission_eligible
                   ) AS missing_count,
                   count(*) FILTER (
                     WHERE commission_eligible
                   ) AS chargeable_operations,
                   min(sale_day_rate) AS min_rate,max(sale_day_rate) AS max_rate,
                   coalesce(sum(
                     CASE WHEN sale_day_rate IS NOT NULL
                            AND commission_eligible
                          THEN round(amount*sale_day_rate*0.05,2)
                          ELSE 0 END
                   ),0) AS fee
            FROM rated GROUP BY currency
          )
          SELECT
            coalesce(jsonb_agg(jsonb_build_object(
              'currency',currency,'gross',gross,
              'operations',operations,'missing',a.missing_count,
              'min_rate',min_rate,'max_rate',max_rate,
              'fee_rub',fee
            ) ORDER BY currency),'[]'::jsonb),
            coalesce(sum(fee) FILTER (WHERE currency<>'XTR'),0),
            coalesce(sum(fee) FILTER (WHERE currency='XTR'),0),
            coalesce(sum(a.missing_count),0),
            coalesce(sum(chargeable_operations) FILTER (
              WHERE currency='XTR'
            ),0)
          INTO rows,fiat_fee,stars_fee,unrated_count,star_count
          FROM aggregates a;

          SELECT coalesce(jsonb_object_agg(
            currency_code,jsonb_build_object(
              'rate',rub_per_unit,'source',source_name,
              'source_date',source_date
            ) ORDER BY currency_code
          ),'{}'::jsonb) INTO today_rates
          FROM public.platform_saas_daily_fx_rates
          WHERE rate_date=(clock_timestamp()
                           AT TIME ZONE 'Europe/Moscow')::date;

          RETURN jsonb_build_object(
            'month',p_month,'rows',rows,
            'fiat_commission_rub',CASE WHEN own_platform THEN 0 ELSE fiat_fee END,
            'stars_estimate_rub',CASE WHEN own_platform THEN 0 ELSE stars_fee END,
            'stars_need_reconciliation',star_count>0,
            'missing_sale_day_rates',unrated_count,
            'next_bot_fee_rub',CASE WHEN own_platform THEN 0 ELSE 150 END,
            'is_platform_owner',own_platform,
            'billing_enabled',coalesce(billing_on,false),
            'today_rates',today_rates,
            'rate_date',(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date,
            'estimate_complete',unrated_count=0 AND star_count=0
              AND (NOT coalesce(billing_on,false) OR (
                first_charge IS NOT NULL AND trial_started IS NOT NULL
              ))
          );
        END;
        $preview$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_quote_project_bill(
            p_project uuid, p_month date, p_pay_currency text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $quote$
        DECLARE
          report jsonb;
          billing_rate numeric;
          rub_total numeric;
          converted numeric;
          quote_ready boolean;
          quote_source_name text;
          quote_source_date date;
        BEGIN
          IF p_pay_currency NOT IN ('RUB','USD','EUR','UAH','BYN')
             OR p_month IS NULL OR extract(day from p_month)<>1 THEN
            RAISE EXCEPTION 'Invalid billing currency/month'
              USING ERRCODE='22023';
          END IF;
          report:=public.platform_saas_project_preview(p_project,p_month);
          rub_total:=round(
            (report->>'fiat_commission_rub')::numeric
            +(report->>'next_bot_fee_rub')::numeric,2
          );
          quote_ready:=coalesce((report->>'estimate_complete')::boolean,false);
          IF p_pay_currency='RUB' THEN
            billing_rate:=1;
          ELSE
            SELECT rub_per_unit,source_name,source_date
              INTO billing_rate,quote_source_name,quote_source_date
            FROM public.platform_saas_daily_fx_rates
            WHERE rate_date=(clock_timestamp()
                             AT TIME ZONE 'Europe/Moscow')::date
              AND currency_code=p_pay_currency
              AND source_name='CBR'
              AND source_date<=rate_date
              AND source_date>=rate_date-interval '14 days';
          END IF;
          quote_ready:=quote_ready AND billing_rate IS NOT NULL;
          IF quote_ready THEN
            converted:=round(rub_total/billing_rate,2);
          END IF;
          RETURN jsonb_build_object(
            'project_id',p_project,'billing_month',p_month,
            'chosen_currency',p_pay_currency,
            'quote_day',(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date,
            'rate_rub_per_unit',billing_rate,
            'total_rub',rub_total,'estimated_total',converted,
            'quote_ready',quote_ready,
            'source_name',CASE WHEN p_pay_currency='RUB' THEN 'fixed'
                               ELSE quote_source_name END,
            'source_date',CASE WHEN p_pay_currency='RUB' THEN NULL
                               ELSE quote_source_date END,
            'pending_stars',coalesce(
              (report->>'stars_need_reconciliation')::boolean,false
            ),
            'missing_rates',coalesce(
              (report->>'missing_daily_rates')::integer,0
            ),
            'billing_enabled',coalesce(
              (report->>'billing_enabled')::boolean,false
            ),
            'is_platform_owner',coalesce(
              (report->>'is_platform_owner')::boolean,false
            ),
            'preview_only',true
          );
        END;
        $quote$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Do not roll back audited daily FX snapshots: invoices require "
        "historically correct rate_date and immutable amounts."
    )
