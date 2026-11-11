"""Immutable monthly FX snapshots for ViMi Pay's dormant commission ledger.

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
    # One published FX policy for each currency and Moscow billing month.
    # Existing invoice lines preserve the exact rate used at issuance.
    op.create_table(
        "platform_saas_fx_rates",
        sa.Column("billing_month", sa.Date(), primary_key=True),
        sa.Column("source_date", sa.Date(), nullable=True),
        sa.Column("source_name", sa.String(40), nullable=False,
                  server_default="owner_verified"),
        # XTR is Telegram's virtual reward unit, not a fiat/crypto
        # asset in the existing CurrencyAsset checkout registry.
        sa.Column("currency_code", sa.String(16), primary_key=True),
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
            "EXTRACT(day FROM billing_month)=1",
            name="ck_saas_fx_first_day",
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
            p_month date, p_currency text, p_rate numeric
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          actor uuid;
          existing record;
        BEGIN
          actor:=public.platform_saas_require_owner();
          IF p_month IS NULL OR extract(day FROM p_month)<>1
             OR p_currency IS NULL OR p_currency='RUB'
             OR p_currency<>upper(btrim(p_currency))
             OR length(p_currency)>16
             OR p_rate IS NULL OR p_rate<=0 OR p_rate>1000000000
             OR p_rate<>round(p_rate,18)
             OR (
               p_currency <> 'XTR'
               AND NOT EXISTS (
                 SELECT 1 FROM public.currency_assets
                 WHERE code=p_currency AND status='active'
               )
             ) THEN
            RAISE EXCEPTION 'Invalid monthly FX policy'
              USING ERRCODE='22023';
          END IF;
          -- Serializes concurrent publications of the same monthly rate.
          PERFORM pg_advisory_xact_lock(
            hashtextextended(
              'vimi-fx:'||to_char(p_month,'YYYY-MM-DD')||':'||p_currency,0
            )
          );
          SELECT rub_per_unit,published_by,published_at INTO existing
          FROM public.platform_saas_fx_rates
          WHERE billing_month=p_month AND currency_code=p_currency;
          IF FOUND THEN
            IF existing.rub_per_unit<>p_rate THEN
              RAISE EXCEPTION 'Monthly FX rate already published'
                USING ERRCODE='23505';
            END IF;
            RETURN jsonb_build_object(
              'month',p_month,'currency',p_currency,
              'rub_per_unit',existing.rub_per_unit,
              'published_at',existing.published_at
            );
          END IF;
          INSERT INTO public.platform_saas_fx_rates(
            billing_month,currency_code,rub_per_unit,published_by
          ) VALUES(p_month,p_currency,p_rate,actor);
          RETURN jsonb_build_object(
            'month',p_month,'currency',p_currency,
            'rub_per_unit',p_rate,
            'published_at',(
              SELECT published_at FROM public.platform_saas_fx_rates
              WHERE billing_month=p_month AND currency_code=p_currency
            )
          );
        END;
        $f$
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_publish_cbr_fx_rate(
            p_month date, p_currency text, p_rate numeric,
            p_source_date date
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $cbr$
        DECLARE
          actor uuid;
          found_rate numeric;
        BEGIN
          actor:=public.platform_saas_require_owner();
          IF p_month IS NULL OR extract(day from p_month)<>1
             OR p_currency NOT IN ('USD','EUR','UAH','BYN')
             OR p_rate IS NULL OR p_rate<=0 OR p_rate>1000000000
             OR p_source_date IS NULL OR p_source_date>p_month
             OR p_source_date<p_month-interval '14 days'
             OR p_rate<>round(p_rate,18) THEN
            RAISE EXCEPTION 'Invalid verified monthly CBR rate'
              USING ERRCODE='22023';
          END IF;
          PERFORM pg_advisory_xact_lock(hashtextextended(
            'vimi-fx:'||to_char(p_month,'YYYY-MM-DD')
            ||':'||p_currency,0
          ));
          SELECT rub_per_unit INTO found_rate
          FROM public.platform_saas_fx_rates
          WHERE billing_month=p_month AND currency_code=p_currency;
          IF FOUND THEN
            IF found_rate<>p_rate THEN
              RAISE EXCEPTION 'Published monthly rate mismatch'
                USING ERRCODE='23505';
            END IF;
          ELSE
            INSERT INTO public.platform_saas_fx_rates(
              billing_month,currency_code,rub_per_unit,published_by,
              source_name,source_date
            ) VALUES(
              p_month,p_currency,p_rate,actor,'CBR',p_source_date
            );
          END IF;
          RETURN jsonb_build_object(
            'month',p_month,'currency',p_currency,
            'rub_per_unit',p_rate,'source_name','CBR',
            'source_date',p_source_date
          );
        END;
        $cbr$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_publish_cbr_fx_rate(date,text,numeric,date) "
        "FROM PUBLIC"
    )
    op.execute("""
        DO $grant_cbr$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles
                    WHERE rolname='membership_runtime') THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_publish_cbr_fx_rate(
                date,text,numeric,date
              ) TO membership_runtime;
          END IF;
        END $grant_cbr$
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_monthly_fx(p_month date)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE rates jsonb;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_month IS NULL OR extract(day FROM p_month)<>1 THEN
            RAISE EXCEPTION 'Invalid FX accounting month'
              USING ERRCODE='22023';
          END IF;
          SELECT COALESCE(jsonb_object_agg(
            currency_code,rub_per_unit ORDER BY currency_code
          ),'{}'::jsonb) INTO rates
          FROM public.platform_saas_fx_rates
          WHERE billing_month=p_month;
          RETURN jsonb_build_object('month',p_month,'rates',rates);
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
              -- Rates are frozen once per billing month, with the same
              -- quote applied to every sale. Invoice lines snapshot FX.
              SELECT rate.rub_per_unit INTO fx
                FROM public.platform_saas_fx_rates rate
               WHERE rate.billing_month=p_month
                 AND rate.currency_code=source.currency_code;
              IF fx IS NULL THEN
                RAISE EXCEPTION 'Missing published monthly FX rate for %',
                  source.currency_code USING ERRCODE='22023';
              END IF;
              -- The old JSON parameter must be empty: one monthly override
              -- must never replace the monthly reference quote.

            END IF;
            IF fx<=0 OR fx>1000000000 THEN
              RAISE EXCEPTION 'Invalid monthly conversion rate'
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
          unrated_count integer;
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
                        ELSE fx.rub_per_unit END AS monthly_rate
            FROM sales s
            LEFT JOIN public.platform_saas_fx_rates fx
              ON fx.billing_month=p_month
             AND fx.currency_code=s.currency
            WHERE s.paid_at >= (p_month::timestamp AT TIME ZONE 'Europe/Moscow')
              AND s.paid_at < ((p_month::timestamp + interval '1 month')
                               AT TIME ZONE 'Europe/Moscow')
          ),
          aggregates AS (
            SELECT currency,count(*) AS operations,
                   sum(amount) AS gross,
                   count(*) FILTER (WHERE monthly_rate IS NULL) AS missing_count,
                   min(monthly_rate) AS min_rate,max(monthly_rate) AS max_rate,
                   coalesce(sum(
                     CASE WHEN monthly_rate IS NOT NULL
                          THEN round(amount*monthly_rate*0.05,2)
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
            coalesce(sum(operations) FILTER (WHERE currency='XTR'),0)
          INTO rows,fiat_fee,stars_fee,unrated_count,star_count
          FROM aggregates a;

          SELECT coalesce(jsonb_object_agg(currency_code,rub_per_unit),
                          '{}'::jsonb) INTO today_rates
          FROM public.platform_saas_fx_rates
          WHERE billing_month=(date_trunc(
            'month',clock_timestamp() AT TIME ZONE 'Europe/Moscow'
          ))::date;

          RETURN jsonb_build_object(
            'month',p_month,'rows',rows,
            'fiat_commission_rub',CASE WHEN own_platform THEN 0 ELSE fiat_fee END,
            'stars_estimate_rub',CASE WHEN own_platform THEN 0 ELSE stars_fee END,
            'stars_need_reconciliation',star_count>0,
            'missing_monthly_rates',unrated_count,
            'next_bot_fee_rub',CASE WHEN own_platform THEN 0 ELSE 150 END,
            'is_platform_owner',own_platform,
            'billing_enabled',coalesce(billing_on,false),
            'month_rates',today_rates,
            'estimate_complete',unrated_count=0 AND star_count=0
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

    # Currency choice is a read-only, owner-scoped quote. No payment is
    # created while commercial rollout is disabled. Real issuance must
    # snapshot the chosen FX, currency and one unified invoice reference.
    op.execute("""
        CREATE FUNCTION public.platform_saas_quote_project_bill(
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
          source_record record;
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
            SELECT rub_per_unit,source_name,source_date INTO source_record
            FROM public.platform_saas_fx_rates
            WHERE billing_month=p_month AND currency_code=p_pay_currency;
            billing_rate:=source_record.rub_per_unit;
          END IF;
          quote_ready:=quote_ready AND billing_rate IS NOT NULL;
          IF quote_ready THEN
            converted:=round(rub_total/billing_rate,2);
          END IF;
          RETURN jsonb_build_object(
            'project_id',p_project,'billing_month',p_month,
            'chosen_currency',p_pay_currency,
            'rate_rub_per_unit',billing_rate,
            'total_rub',rub_total,'estimated_total',converted,
            'quote_ready',quote_ready,
            'source_name',CASE WHEN p_pay_currency='RUB' THEN 'fixed'
                               ELSE source_record.source_name END,
            'source_date',CASE WHEN p_pay_currency='RUB' THEN NULL
                               ELSE source_record.source_date END,
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
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_quote_project_bill(uuid,date,text) FROM PUBLIC"
    )
    op.execute("""
        DO $grant_quote$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles
                    WHERE rolname='membership_runtime') THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_quote_project_bill(uuid,date,text)
            TO membership_runtime;
          END IF;
        END $grant_quote$
    """)

    for signature in (
        "platform_saas_publish_fx_rate(date,text,numeric)",
        "platform_saas_monthly_fx(date)",
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
              public.platform_saas_monthly_fx(date)
              TO membership_runtime;
          END IF;
        END $grant$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Published FX snapshots and billing function cannot be silently "
        "rolled back: historical invoice-rate correctness must be preserved."
    )
