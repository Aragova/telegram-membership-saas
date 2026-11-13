"""Existing real accounts follow new commercial free-grant quotas.

Revision ID: 0072_saas_grants
Revises: 0071_saas_prices
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0072_saas_grants"
down_revision: str | None = "0071_saas_prices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The 72-hour trial claim records actual ownership/history. Newly
    # activated legacy accounts have a separate non-resettable enrollment
    # event, so their future free grants can no longer bypass quota.
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_complimentary_grant_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          effective_at timestamptz;
          max_per_month integer;
          recorded_owner uuid;
          recorded_merchant uuid;
          actor uuid;
          selected_project uuid;
          channel_id text;
          now_at timestamptz;
          month_begin date;
          used_count integer;
          owner_is_operator boolean;
        BEGIN
          IF TG_OP<>'INSERT' OR NEW.source<>'manual' THEN RETURN NEW; END IF;
          SELECT effective_from,monthly_complimentary_limit
            INTO effective_at,max_per_month
          FROM public.platform_saas_access_policy
          WHERE id=1 AND enabled=true;
          now_at:=clock_timestamp();
          IF effective_at IS NULL OR now_at<effective_at THEN RETURN NEW; END IF;
          SELECT p.owner_user_id,p.merchant_id
            INTO recorded_owner,recorded_merchant
          FROM public.projects p WHERE p.id=NEW.project_id;
          IF recorded_owner IS NULL OR recorded_merchant IS NULL THEN
            RAISE EXCEPTION 'Project owner missing' USING ERRCODE='42501';
          END IF;

          -- A missing trial claim does NOT prove an owner is legacy.
          -- The access policy can be enabled after its effective_from:
          -- previously connected, unclaimed post-cutoff accounts are
          -- commercial and must consume the same free-grant quota.
          SELECT EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=recorded_owner AND o.role='owner'
          ) INTO owner_is_operator;
          IF NOT owner_is_operator AND NOT EXISTS(
            SELECT 1 FROM public.merchant_platform_trial_claims t
            WHERE t.owner_user_id=recorded_owner AND t.scope='owner'
              AND t.policy_key='vimi-saas-72h-v1'
              AND t.claimed_at>=effective_at
          ) AND NOT EXISTS(
            SELECT 1 FROM public.platform_saas_commercial_enrollments e
            WHERE e.owner_user_id=recorded_owner
              AND e.effective_from>=effective_at
          ) AND public.platform_saas_owner_was_legacy(
              recorded_owner,effective_at
          ) THEN RETURN NEW; END IF;

          actor := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          selected_project := NULLIF(
            current_setting('app.current_project_id',true),''
          )::uuid;
          IF actor IS DISTINCT FROM recorded_owner OR
             selected_project IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION 'Complimentary grants require project owner'
              USING ERRCODE='42501';
          END IF;

          SELECT r.external_id INTO channel_id
            FROM public.resources r
            WHERE r.id=NEW.resource_id AND r.project_id=NEW.project_id
              AND r.resource_type IN ('telegram_channel','telegram_group')
              AND r.status='active' AND r.removed_at IS NULL;
          IF channel_id IS NULL THEN
            RAISE EXCEPTION 'An active channel is required for free grant'
              USING ERRCODE='23514';
          END IF;
          -- Project owner is the platform operator, not a moderator.
          -- All of their "My Bots" channels skip tenant grant quotas.
          IF owner_is_operator THEN RETURN NEW; END IF;

          month_begin:=(now_at AT TIME ZONE 'Europe/Moscow')::date;
          month_begin:=date_trunc('month',month_begin::timestamp)::date;
          PERFORM pg_advisory_xact_lock(hashtextextended(
            'saas-free:'||recorded_owner::text||':'||channel_id||':'||
              to_char(month_begin,'YYYY-MM-DD'),0
          ));
          SELECT count(*) INTO used_count
            FROM public.platform_saas_complimentary_events c
          WHERE c.owner_user_id=recorded_owner
            AND c.channel_chat_id=channel_id AND c.month_start=month_begin;
          IF used_count>=max_per_month THEN
            RAISE EXCEPTION 'Monthly complimentary grant quota reached'
              USING ERRCODE='P6702';
          END IF;
          INSERT INTO public.platform_saas_complimentary_events(
            id,owner_user_id,merchant_id,project_id,channel_chat_id,
            customer_id,grant_id,source_id,kind,month_start,occurred_at
          ) VALUES (
            gen_random_uuid(),recorded_owner,recorded_merchant,NEW.project_id,
            channel_id,NEW.customer_id,NEW.id,NEW.source_id,
            CASE WHEN NEW.manual_plan_id IS NULL THEN 'manual' ELSE 'plan' END,
            month_begin,now_at
          );
          RETURN NEW;
        END;
        $guard$""")
    op.execute("""CREATE OR REPLACE FUNCTION public.platform_saas_manual_grant_update_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          effective_at timestamptz;
          account_owner uuid;
        BEGIN
          IF OLD.source<>'manual' AND NEW.source<>'manual' THEN RETURN NEW; END IF;
          SELECT effective_from INTO effective_at
          FROM public.platform_saas_access_policy
          WHERE id=1 AND enabled=true;
          IF effective_at IS NULL OR clock_timestamp()<effective_at
          THEN RETURN NEW; END IF;
          SELECT p.owner_user_id INTO account_owner
          FROM public.projects p WHERE p.id=OLD.project_id;
          IF account_owner IS NULL THEN RETURN NEW; END IF;
          -- Preserve pre-rollout legacy entitlements, but do NOT let an
          -- unclaimed post-cutoff account mutate grants to avoid quotas.
          IF NOT EXISTS(
              SELECT 1 FROM public.merchant_platform_trial_claims t
              WHERE t.owner_user_id=account_owner AND t.scope='owner'
                AND t.policy_key='vimi-saas-72h-v1'
                AND t.claimed_at>=effective_at
            ) AND NOT EXISTS(
              SELECT 1 FROM public.telegram_users u
              JOIN public.platform_operators o
                ON o.telegram_user_id=u.telegram_user_id
              WHERE u.id=account_owner AND o.role='owner'
            ) AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_commercial_enrollments e
              WHERE e.owner_user_id=account_owner
                AND e.effective_from>=effective_at
            ) AND public.platform_saas_owner_was_legacy(
              account_owner,effective_at
            )
          THEN RETURN NEW; END IF;
          IF (OLD.id,OLD.project_id,OLD.customer_id,OLD.resource_id,
              OLD.source,OLD.source_id,OLD.manual_plan_id,
              OLD.valid_from,OLD.valid_until,OLD.created_at)
             IS DISTINCT FROM
             (NEW.id,NEW.project_id,NEW.customer_id,NEW.resource_id,
              NEW.source,NEW.source_id,NEW.manual_plan_id,
              NEW.valid_from,NEW.valid_until,NEW.created_at)
          THEN
            RAISE EXCEPTION 'Manual grant identity and validity are immutable'
              USING ERRCODE='42501';
          END IF;
          IF OLD.status='revoked' AND NEW.status<>'revoked' THEN
            RAISE EXCEPTION 'Revoked manual access cannot be reactivated'
              USING ERRCODE='42501';
          END IF;
          IF OLD.status='active' AND NEW.status='active' AND
             NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
            RAISE EXCEPTION 'Manual grant revocation invalid'
              USING ERRCODE='42501';
          END IF;
          IF OLD.status='revoked' AND
             NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
            RAISE EXCEPTION 'Manual grant revocation immutable'
              USING ERRCODE='42501';
          END IF;
          RETURN NEW;
        END;
        $guard$""")


def downgrade() -> None:
    raise RuntimeError(
        "Do not reinstate complimentary quota bypasses for enrolled accounts"
    )
