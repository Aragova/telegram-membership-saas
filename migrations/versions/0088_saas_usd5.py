"""Forward-only USD 5 service fee for new ViMi Pay invoices.

Revision ID: 0088_saas_usd5
Revises: 0087_saas_review_result_notice

A new monthly issuance is $5 with immutable CBR sale-date rounding. Existing
$3 invoices, their lines, settled bank receipts and historical partial payments
remain unchanged. The guard accepts both legacy $3 and new $5 bot-fee lines,
but issuance only creates $5 lines going forward.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0088_saas_usd5"
down_revision: str | None = "0087_saas_review_result_notice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing SECURITY DEFINER signatures, permissions and RLS are retained.
    op.execute("""CREATE FUNCTION public.platform_saas_price_profile_read()
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; currency text;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      IF actor IS NULL THEN
        RAISE EXCEPTION 'Account is required' USING ERRCODE='42501';
      END IF;
      SELECT currency_code INTO currency
      FROM public.platform_saas_price_profiles WHERE owner_user_id=actor;
      currency:=coalesce(currency,'RUB');
      RETURN jsonb_build_object(
        'currency',currency,
        'monthly_amount',CASE WHEN currency='USD' THEN '5.00'
                              ELSE '150.00' END
      );
    END; $f$""")
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
                  AND NEW.original_amount IN (3,5)
                  AND bot_fee_usd_fx IS NOT NULL
                  AND NEW.rub_per_unit=bot_fee_usd_fx
                  AND NEW.charge_rub=round(NEW.original_amount*bot_fee_usd_fx,2))
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
                     LEFT JOIN public.platform_saas_bot_service_activations a
                       ON a.invoice_id=prior.id
                     WHERE prior.bot_instance_id=parent.bot_instance_id
                       AND prior.owner_user_id=parent.owner_user_id
                       AND prior.kind='prepayment' AND prior.status='issued'
                       AND coalesce(a.effective_end,prior.period_end)=
                           parent.period_start
                       AND sale.paid_at>=coalesce(
                           a.effective_start,prior.period_start)
                       AND sale.paid_at<coalesce(
                           a.effective_end,prior.period_end)
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
        $guard$""")
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_issue_bot_invoice(
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
          SELECT coalesce(
            (SELECT currency_code FROM public.platform_saas_price_profiles
             WHERE owner_user_id=entry.owner_user_id),
            'RUB'
          ) INTO currency;
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
        $f$""")


def downgrade() -> None:
    raise RuntimeError("Do not change the price of already issued financial records")
