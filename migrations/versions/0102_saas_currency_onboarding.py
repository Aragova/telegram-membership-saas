"""Explicit first-time per-bot service currency selection, without repricing history.

Revision ID: 0102_saas_currency_onboarding
Revises: 0101_saas_provider_refunds

Existing registered bots are confirmed. Only newly registered bots require the
explicit last-step confirmation of the inherited safe RUB/USD default.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0102_saas_currency_onboarding"
down_revision: str | None = "0101_saas_provider_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Keep the existing canonical billing function intact except for the
# currency-confirmation guard immediately after its bot advisory lock.
# Downgrade restores the exact pre-0102 definition (no invoice mutations).
_ISSUER_CURRENCY_GUARD = """
          -- Newly registered bots must explicitly choose their own future
          -- service currency before the first immutable invoice is issued.
          -- The check is under the same canonical per-bot advisory lock.
          IF NOT EXISTS (
            SELECT 1 FROM public.platform_saas_bot_price_profiles bp
            WHERE bp.bot_instance_id=p_bot
              AND bp.currency_selected
          ) THEN
            RAISE EXCEPTION 'Confirm bot service currency before billing'
              USING ERRCODE='42501';
          END IF;"""
_ISSUER_CURRENCY_CONFIRMED_SQL = """
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
          -- Newly registered bots must explicitly choose their own future
          -- service currency before the first immutable invoice is issued.
          -- The check is under the same canonical per-bot advisory lock.
          IF NOT EXISTS (
            SELECT 1 FROM public.platform_saas_bot_price_profiles bp
            WHERE bp.bot_instance_id=p_bot
              AND bp.currency_selected
          ) THEN
            RAISE EXCEPTION 'Confirm bot service currency before billing'
              USING ERRCODE='42501';
          END IF;
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
"""


def upgrade() -> None:
    # Existing bots keep their price without re-onboarding or changing
    # any invoice. Future bot profiles have an unconfirmed default.
    op.execute("""
ALTER TABLE public.platform_saas_bot_price_profiles
 ADD COLUMN currency_selected boolean NOT NULL DEFAULT false
    """)
    op.execute("""
UPDATE public.platform_saas_bot_price_profiles SET currency_selected=true
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_bot_currency_selection_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public AS $f$
BEGIN
 NEW.currency_selected:=true;
 RETURN NEW;
END; $f$
    """)
    op.execute("""
CREATE TRIGGER platform_saas_bot_currency_selection_confirmed
 BEFORE UPDATE OF currency_code
 ON public.platform_saas_bot_price_profiles
 FOR EACH ROW EXECUTE FUNCTION
 public.platform_saas_bot_currency_selection_guard()
    """)
    # The existing authenticated reader guards actor and project ownership.
    # No new direct table-read grant is added to membership_runtime.
    op.execute("""
CREATE FUNCTION public.platform_saas_bot_price_profile_read_v2(
 p_project uuid
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE summary jsonb; selected boolean;
BEGIN
 summary:=public.platform_saas_bot_price_profile_read(p_project);
 SELECT bp.currency_selected INTO STRICT selected
 FROM public.platform_saas_bot_price_profiles bp
 WHERE bp.bot_instance_id=(summary->>'bot_id')::uuid;
 RETURN summary || jsonb_build_object('currency_selected',selected);
EXCEPTION WHEN NO_DATA_FOUND OR TOO_MANY_ROWS THEN
 RAISE EXCEPTION 'Invalid bot price profile' USING ERRCODE='42501';
END; $f$
    """)
    op.execute("""
REVOKE ALL ON FUNCTION
 public.platform_saas_bot_price_profile_read_v2(uuid) FROM PUBLIC
    """)
    op.execute("""
DO $r$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
   GRANT EXECUTE ON FUNCTION
    public.platform_saas_bot_price_profile_read_v2(uuid)
   TO membership_runtime;
 END IF;
END $r$
    """)
    op.execute(_ISSUER_CURRENCY_CONFIRMED_SQL)


def downgrade() -> None:
    # Restore the pre-selection canonical issuer before dropping the column.
    op.execute(
        _ISSUER_CURRENCY_CONFIRMED_SQL.replace(_ISSUER_CURRENCY_GUARD, "")
    )
    op.execute("""
DROP FUNCTION IF EXISTS public.platform_saas_bot_price_profile_read_v2(uuid)
    """)
    op.execute("""
DROP TRIGGER IF EXISTS platform_saas_bot_currency_selection_confirmed
 ON public.platform_saas_bot_price_profiles
    """)
    op.execute("""
DROP FUNCTION IF EXISTS public.platform_saas_bot_currency_selection_guard()
    """)
    op.execute("""
ALTER TABLE public.platform_saas_bot_price_profiles
 DROP COLUMN IF EXISTS currency_selected
    """)
