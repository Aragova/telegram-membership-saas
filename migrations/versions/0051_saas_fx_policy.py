"""Immutable per-payment-day FX snapshots for ViMi Pay's dormant commission ledger.

Revision ID: 0051_saas_fx_policy
Revises: 0050_my_bots_exempt

Does NOT enable billing, trial, holds, issue invoices or alter purchases.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_saas_fx_policy"
down_revision: str | None = "0050_my_bots_exempt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # One published FX policy for each currency and Moscow payment date.
    # Existing invoice lines preserve the exact rate used at issuance.
    op.create_table(
        "platform_saas_fx_rates",
        sa.Column("rate_date", sa.Date(), primary_key=True),
        sa.Column(
            "currency_code", sa.String(16),
            sa.ForeignKey("currency_assets.code", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("rub_per_unit", sa.Numeric(38, 18), nullable=False),
        sa.Column(
            "published_by", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "currency_code<>'RUB' AND rub_per_unit>0 "
            "AND rub_per_unit<=1000000000",
            name="ck_saas_fx_rate_valid",
        ),
    )
    op.execute(
        "ALTER TABLE public.platform_saas_fx_rates "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_fx_rates "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
        DO $revoke$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            REVOKE ALL ON TABLE public.platform_saas_fx_rates
              FROM membership_runtime;
          END IF;
        END $revoke$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_fx_immutable()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $f$
        BEGIN
          RAISE EXCEPTION 'Published FX rate is immutable'
            USING ERRCODE='42501';
        END;
        $f$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_fx_immutable
          BEFORE UPDATE OR DELETE ON public.platform_saas_fx_rates
          FOR EACH ROW EXECUTE FUNCTION public.platform_saas_fx_immutable()
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_publish_fx_rate(
            p_day date, p_currency text, p_rate numeric
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          actor uuid;
          existing record;
        BEGIN
          actor:=public.platform_saas_require_owner();
          IF p_day IS NULL
             OR p_currency IS NULL OR p_currency='RUB'
             OR p_currency<>upper(btrim(p_currency))
             OR length(p_currency)>16
             OR p_rate IS NULL OR p_rate<=0 OR p_rate>1000000000
             OR p_rate<>round(p_rate,18)
             OR NOT EXISTS (
               SELECT 1 FROM public.currency_assets
               WHERE code=p_currency AND status='active'
             ) THEN
            RAISE EXCEPTION 'Invalid daily FX policy'
              USING ERRCODE='22023';
          END IF;
          -- Serializes concurrent publications of the same monthly rate.
          PERFORM pg_advisory_xact_lock(
            hashtextextended(
              'vimi-fx:'||to_char(p_day,'YYYY-MM-DD')||':'||p_currency,0
            )
          );
          SELECT rub_per_unit,published_by,published_at INTO existing
          FROM public.platform_saas_fx_rates
          WHERE rate_date=p_day AND currency_code=p_currency;
          IF FOUND THEN
            IF existing.rub_per_unit<>p_rate THEN
              RAISE EXCEPTION 'Daily FX rate already published'
                USING ERRCODE='23505';
            END IF;
            RETURN jsonb_build_object(
              'day',p_day,'currency',p_currency,
              'rub_per_unit',existing.rub_per_unit,
              'published_at',existing.published_at
            );
          END IF;
          INSERT INTO public.platform_saas_fx_rates(
            rate_date,currency_code,rub_per_unit,published_by
          ) VALUES(p_day,p_currency,p_rate,actor);
          RETURN jsonb_build_object(
            'day',p_day,'currency',p_currency,
            'rub_per_unit',p_rate,
            'published_at',(
              SELECT published_at FROM public.platform_saas_fx_rates
              WHERE rate_date=p_day AND currency_code=p_currency
            )
          );
        END;
        $f$
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_day_fx(p_day date)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE rates jsonb;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_day IS NULL THEN
            RAISE EXCEPTION 'Invalid FX accounting day'
              USING ERRCODE='22023';
          END IF;
          SELECT COALESCE(jsonb_object_agg(
            currency_code,rub_per_unit ORDER BY currency_code
          ),'{}'::jsonb) INTO rates
          FROM public.platform_saas_fx_rates
          WHERE rate_date=p_day;
          RETURN jsonb_build_object('day',p_day,'rates',rates);
        END;
        $f$
    """)

    # Preserve the existing owner-only grant and original RPC signature.
    # SQL amount/currency/source/bot attribution logic stays unchanged.
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
              -- Never trust one-off caller-supplied rates: all non-RUB
              -- conversions come from immutable owner-published daily
              -- FX records. The source line still snapshots the rate.
              SELECT rate.rub_per_unit INTO fx
                FROM public.platform_saas_fx_rates rate
               WHERE rate.rate_date=(source.paid_at AT TIME ZONE 'Europe/Moscow')::date
                 AND rate.currency_code=source.currency_code;
              IF fx IS NULL THEN
                RAISE EXCEPTION 'Missing published payment-day FX rate for %',
                  source.currency_code USING ERRCODE='22023';
              END IF;
              -- The old JSON parameter must be empty: one monthly override
              -- must never replace the rate of each sale's actual day.

            END IF;
            IF fx<=0 OR fx>1000000000 THEN
              RAISE EXCEPTION 'Invalid daily conversion rate'
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
    # One owner-only, READ-ONLY account-scope card in the connected bot.
    # No invoice creation or claims that Stars reward amounts are settled.
    op.execute("""
        CREATE FUNCTION public.platform_saas_project_preview(
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
          missing integer;
          star_count integer;
          today_rates jsonb;
          billing_on boolean;
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
          SELECT enabled INTO billing_on
            FROM public.platform_saas_billing_control WHERE id=1;

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
                        ELSE fx.rub_per_unit END AS daily_rate
            FROM sales s
            LEFT JOIN public.platform_saas_fx_rates fx
              ON fx.rate_date=(s.paid_at AT TIME ZONE 'Europe/Moscow')::date
             AND fx.currency_code=s.currency
            WHERE s.paid_at >= (p_month::timestamp AT TIME ZONE 'Europe/Moscow')
              AND s.paid_at < ((p_month::timestamp + interval '1 month')
                               AT TIME ZONE 'Europe/Moscow')
          ),
          aggregates AS (
            SELECT currency,count(*) AS operations,
                   sum(amount) AS gross,
                   count(*) FILTER (WHERE daily_rate IS NULL) AS missing,
                   min(daily_rate) AS min_rate,max(daily_rate) AS max_rate,
                   coalesce(sum(
                     CASE WHEN daily_rate IS NOT NULL
                          THEN round(amount*daily_rate*0.05,2)
                          ELSE 0 END
                   ),0) AS fee
            FROM rated GROUP BY currency
          )
          SELECT
            coalesce(jsonb_agg(jsonb_build_object(
              'currency',currency,'gross',gross,
              'operations',operations,'missing',missing,
              'min_rate',min_rate,'max_rate',max_rate,
              'fee_rub',fee
            ) ORDER BY currency),'[]'::jsonb),
            coalesce(sum(fee) FILTER (WHERE currency<>'XTR'),0),
            coalesce(sum(fee) FILTER (WHERE currency='XTR'),0),
            coalesce(sum(missing),0),
            coalesce(sum(operations) FILTER (WHERE currency='XTR'),0)
          INTO rows,fiat_fee,stars_fee,missing,star_count
          FROM aggregates;

          SELECT coalesce(jsonb_object_agg(currency_code,rub_per_unit),
                          '{}'::jsonb) INTO today_rates
          FROM public.platform_saas_fx_rates
          WHERE rate_date=(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date;

          RETURN jsonb_build_object(
            'month',p_month,'rows',rows,
            'fiat_commission_rub',CASE WHEN own_platform THEN 0 ELSE fiat_fee END,
            'stars_estimate_rub',CASE WHEN own_platform THEN 0 ELSE stars_fee END,
            'stars_need_reconciliation',star_count>0,
            'missing_daily_rates',missing,
            'next_bot_fee_rub',CASE WHEN own_platform THEN 0 ELSE 150 END,
            'is_platform_owner',own_platform,
            'billing_enabled',coalesce(billing_on,false),
            'today_rates',today_rates,
            'estimate_complete',missing=0 AND star_count=0
          );
        END;
        $preview$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_project_preview(uuid,date) FROM PUBLIC"
    )
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles
                     WHERE rolname='membership_runtime') THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_project_preview(uuid,date)
              TO membership_runtime;
          END IF;
        END $grants$
    """)

    for signature in (
        "platform_saas_publish_fx_rate(date,text,numeric)",
        "platform_saas_day_fx(date)",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC"
        )
    op.execute("""
        DO $grant$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_publish_fx_rate(date,text,numeric),
              public.platform_saas_day_fx(date)
              TO membership_runtime;
          END IF;
        END $grant$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Published FX snapshots and billing function cannot be silently "
        "rolled back: historical invoice-rate correctness must be preserved."
    )
