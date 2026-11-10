"""Opt-in once-per-owner 72h trial and complimentary grant anti-abuse.

Revision ID: 0045_saas_trial_grant_quota
Revises: 0044_saas_billing_operations

This rollout is intentionally OFF by default. Existing projects and their
paid access, bot polling, payments and entitlements are not modified.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_saas_trial_grant_quota"
down_revision: str | None = "0044_saas_billing_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "vimi-saas-72h-v1"


def upgrade() -> None:
    op.create_table(
        "platform_saas_access_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("monthly_complimentary_limit", sa.Integer(),
                  nullable=False, server_default="3"),
        sa.CheckConstraint("id=1", name="ck_saas_access_policy_singleton"),
        sa.CheckConstraint(
            "NOT enabled OR effective_from IS NOT NULL",
            name="ck_saas_access_start_required",
        ),
        sa.CheckConstraint(
            "monthly_complimentary_limit BETWEEN 0 AND 100",
            name="ck_saas_complimentary_limit",
        ),
    )
    op.execute("""
        INSERT INTO public.platform_saas_access_policy
        (id, enabled, effective_from, monthly_complimentary_limit)
        VALUES (1, false, NULL, 3)
    """)

    # Write-only, durable, non-erasable audit for complimentary MANUAL grants.
    # One activation of a multi-channel plan uses one source_id across grants,
    # but consumes exactly one slot in EACH distinct target channel.
    op.create_table(
        "platform_saas_complimentary_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_user_id", sa.Uuid(),
                  sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("merchant_id", sa.Uuid(),
                  sa.ForeignKey("merchant_identities.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("channel_chat_id", sa.String(32), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint(
            "owner_user_id", "channel_chat_id", "source_id",
            name="uq_saas_complimentary_channel_activation",
        ),
        sa.CheckConstraint(
            "kind IN ('manual','plan')", name="ck_saas_complimentary_kind",
        ),
    )
    op.create_index(
        "ix_saas_complimentary_owner_channel_month",
        "platform_saas_complimentary_events",
        ["owner_user_id", "channel_chat_id", "month_start"],
    )
    for name in ("platform_saas_access_policy", "platform_saas_complimentary_events"):
        op.execute(f"ALTER TABLE public.{name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{name} FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $revoke$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
            REVOKE ALL ON
              public.platform_saas_access_policy,
              public.platform_saas_complimentary_events
            FROM membership_runtime;
          END IF;
        END $revoke$
    """)

    # Registration is already atomic with project creation in both token
    # and Managed Bots flows. DB trigger works regardless of Python handler.
    op.execute("""
        CREATE FUNCTION public.platform_saas_first_bot_trial_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          effective_at timestamptz;
          recorded_owner uuid;
          recorded_merchant uuid;
          started timestamptz;
          actor uuid;
          legacy_bot boolean;
          other_connected boolean;
        BEGIN
          IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
          SELECT effective_from INTO effective_at
          FROM public.platform_saas_access_policy
          WHERE id=1 AND enabled=true;
          IF effective_at IS NULL OR clock_timestamp()<effective_at
             OR NEW.created_at<effective_at THEN RETURN NEW; END IF;
          SELECT p.owner_user_id,p.merchant_id
            INTO recorded_owner,recorded_merchant
          FROM public.projects p WHERE p.id=NEW.project_id;
          IF recorded_owner IS NULL OR recorded_merchant IS NULL THEN
            RAISE EXCEPTION 'Verified project owner required'
              USING ERRCODE='42501';
          END IF;

          -- Owner accounts with bots before rollout remain grandfathered;
          -- no retroactive trial, invoices or quota. Deleted old bots remain
          -- detectable via permanent usage events + historical owner periods.
          SELECT EXISTS(
            SELECT 1 FROM public.platform_usage_events ev
            JOIN public.merchant_project_periods mp
              ON mp.project_id=ev.project_id
             AND mp.owner_user_id=recorded_owner
             AND mp.started_at<=ev.occurred_at
             AND (mp.ended_at IS NULL OR mp.ended_at>=ev.occurred_at)
            WHERE ev.event_type='bot_connected'
              AND ev.occurred_at<effective_at
          ) INTO legacy_bot;
          IF legacy_bot THEN RETURN NEW; END IF;

          -- A platform operator can administer third-party channels.
          -- Do not auto-claim a platform owner's personal trial or grant
          -- an exemption from a mere role flag; require owner review later.
          IF EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=recorded_owner AND o.role='owner'
          ) THEN RETURN NEW; END IF;

          actor := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF actor IS NOT NULL AND actor<>recorded_owner THEN
            RAISE EXCEPTION 'Trial must belong to actual project owner'
              USING ERRCODE='42501';
          END IF;
          PERFORM pg_advisory_xact_lock(
            hashtextextended('saas-trial-owner:'||recorded_owner::text,0)
          );
          SELECT t.claimed_at INTO started
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=recorded_owner AND t.scope='owner'
            AND t.policy_key='vimi-saas-72h-v1';

          IF started IS NULL THEN
            -- No duplicate trial after reconnect. A second owner project
            -- cannot win this race due to the transaction-scoped lock and
            -- database partial UNIQUE index on owner + policy key.
            INSERT INTO public.merchant_platform_trial_claims(
              id,merchant_id,owner_user_id,policy_key,scope,claimed_at
            ) VALUES (
              gen_random_uuid(),recorded_merchant,recorded_owner,
              'vimi-saas-72h-v1','owner',clock_timestamp()
            );
            RETURN NEW;
          END IF;
          IF clock_timestamp()<started+interval '72 hours' THEN
            SELECT EXISTS(
              SELECT 1 FROM public.bot_instances b
              JOIN public.projects p ON p.id=b.project_id
              WHERE p.owner_user_id=recorded_owner AND b.id<>NEW.id
                AND b.status<>'deleting'
            ) INTO other_connected;
            IF other_connected THEN
              RAISE EXCEPTION 'Only one bot is allowed during SaaS trial'
                USING ERRCODE='P6703';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_first_bot_trial
        AFTER INSERT ON public.bot_instances
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_first_bot_trial_guard()
    """)

    # Fire at actual DB grant insertion, not just the UI callback.
    # The manual plan path inserts one grant per resource with a shared
    # source ID, so the quota is per channel, and both paths share one pool.
    op.execute("""
        CREATE FUNCTION public.platform_saas_complimentary_grant_guard()
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
          original_bot uuid;
          bots_count integer;
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

          -- Existing connected owners are grandfathered until specifically
          -- enrolled later. Only owners whose trial began after rollout are
          -- subject to new quotas; no surprise limits to active projects.
          IF NOT EXISTS(
            SELECT 1 FROM public.merchant_platform_trial_claims t
            WHERE t.owner_user_id=recorded_owner AND t.scope='owner'
              AND t.policy_key='vimi-saas-72h-v1'
              AND t.claimed_at>=effective_at
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
          SELECT EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=recorded_owner AND o.role='owner'
          ) INTO owner_is_operator;
          IF owner_is_operator THEN
            SELECT count(*),max(b.id::text)::uuid
              INTO bots_count,original_bot FROM public.bot_instances b
            WHERE b.project_id=NEW.project_id AND b.status<>'deleting';
            IF bots_count=1 AND EXISTS(
              SELECT 1 FROM public.platform_saas_owner_exemptions e
              WHERE e.bot_instance_id=original_bot
                AND e.project_id=NEW.project_id
                AND e.owner_user_id=recorded_owner
                AND e.merchant_id=recorded_merchant
                AND e.channel_chat_id=channel_id
                AND e.verified_at<=now_at
                AND (e.ended_at IS NULL OR e.ended_at>now_at)
            ) THEN RETURN NEW; END IF;
          END IF;

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
        CREATE TRIGGER trg_saas_complimentary_grant
        AFTER INSERT ON public.entitlement_grants
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_complimentary_grant_guard()
    """)

    # Financial history and the immutable grant/claim log are not deletable.
    op.execute("""
        CREATE FUNCTION public.platform_saas_immutable_event_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        BEGIN
          RAISE EXCEPTION 'Complimentary grant audit cannot be changed or erased'
            USING ERRCODE='42501';
        END;
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_complimentary_event_immutable
        BEFORE UPDATE OR DELETE ON public.platform_saas_complimentary_events
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_immutable_event_guard()
    """)
    for fn in (
        "platform_saas_first_bot_trial_guard()",
        "platform_saas_complimentary_grant_guard()",
        "platform_saas_immutable_event_guard()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn} FROM PUBLIC")


def downgrade() -> None:
    op.execute("""
        DO $safe$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM public.platform_saas_access_policy WHERE enabled
          ) OR EXISTS(
            SELECT 1 FROM public.platform_saas_complimentary_events
          ) THEN
            RAISE EXCEPTION 'Cannot roll back activated trial/quota history';
          END IF;
        END $safe$
    """)
    for table, trigger in (
        ("platform_saas_complimentary_events",
         "trg_saas_complimentary_event_immutable"),
        ("entitlement_grants", "trg_saas_complimentary_grant"),
        ("bot_instances", "trg_saas_first_bot_trial"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON public.{table}")
    for fn in (
        "platform_saas_immutable_event_guard()",
        "platform_saas_complimentary_grant_guard()",
        "platform_saas_first_bot_trial_guard()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{fn}")
    op.drop_table("platform_saas_complimentary_events")
    op.drop_table("platform_saas_access_policy")
