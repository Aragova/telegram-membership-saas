"""Platform operator's My Bots are always SaaS-exempt. Remove redundant audit RPCs.

Revision ID: 0050_platform_owner_my_bots_exempt
Revises: 0049_saas_exemption_pages

No billing activation, invoices, tenant access changes or backfill.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0050_platform_owner_my_bots_exempt"
down_revision: str | None = "0049_saas_exemption_pages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Historical, project-bound owner match. The same rule is reused by
    # fee invoices, commissions, line guards, checkout and hold gates.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.platform_saas_exemption_state(
            p_project uuid, p_bot uuid, p_merchant uuid,
            p_owner uuid, p_effective timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
          exempt record;
          channel_count integer;
          matched boolean;
        BEGIN
          -- ViMi Pay account rule: anything registered in "My Bots" under
          -- the authenticated platform owner is automatically SaaS-exempt.
          -- Use the historical project owner period, not mere admin access
          -- or mutable bot names. Transfers change future status only.
          IF EXISTS(
            SELECT 1
              FROM public.platform_operators po
              JOIN public.telegram_users u
                ON u.telegram_user_id=po.telegram_user_id
              JOIN public.merchant_project_periods mp
                ON mp.owner_user_id=u.id
               AND mp.project_id=p_project
               AND mp.merchant_id=p_merchant
               AND mp.started_at<=p_effective
               AND (mp.ended_at IS NULL OR mp.ended_at>p_effective)
              JOIN public.bot_instances b
                ON b.id=p_bot AND b.project_id=p_project
             WHERE po.role='owner' AND u.id=p_owner
          ) THEN
            RETURN 'exempt';
          END IF;
          SELECT * INTO exempt
            FROM public.platform_saas_owner_exemptions e
           WHERE e.project_id=p_project
             AND e.bot_instance_id=p_bot
             AND e.merchant_id=p_merchant
             AND e.owner_user_id=p_owner
             AND e.verified_at<=p_effective
             AND (e.ended_at IS NULL OR e.ended_at>p_effective)
           ORDER BY e.verified_at DESC LIMIT 1;
          IF NOT FOUND THEN
            RETURN 'not_exempt';
          END IF;
          SELECT count(*),
                 bool_or(c.telegram_chat_id=exempt.channel_chat_id)
            INTO channel_count,matched
            FROM public.merchant_channel_periods c
           WHERE c.project_id=p_project AND c.merchant_id=p_merchant
             AND c.started_at<=p_effective
             AND (c.ended_at IS NULL OR c.ended_at>p_effective);
          IF channel_count <> 1 OR matched IS DISTINCT FROM true THEN
            RETURN 'needs_review';
          END IF;
          RETURN 'exempt';
        END;
        $function$
    """)
    # An operator-owned project already has a checked-in authenticated
    # project owner. No separate Telegram creator audit is needed.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.platform_saas_complimentary_grant_guard()
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
        $guard$
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
          -- No SaaS debt hold for the ViMi Pay operator's own account.
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
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_receipts r
              WHERE r.invoice_id=i.id
            );
          IF overdue>0 THEN RETURN 'held'; END IF;
          IF pending>0 THEN RETURN 'grace'; END IF;
          RETURN 'clear';
        END;
        $f$
    """)
    # Obsolete UI-only inventory functions are no longer used.
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_exemption_preflight_page(integer,integer)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_exemption_preflight()"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Refuse rollback to a policy requiring Telegram creator proof: "
        "financial exemptions must not change retroactively."
    )
