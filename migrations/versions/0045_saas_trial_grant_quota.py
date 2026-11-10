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

    # Platform usage metering (migration 0020) persists the bot_connected
    # event's occurred_at from bot_instances.created_at. The runtime can
    # supply that column, so an attacker could backdate a connection made
    # while billing was disabled and later claim pre-rollout grandfathering.
    # Normalize new bot timestamps at insertion even while SaaS access policy
    # is OFF; existing rows and historical events are NOT modified.
    op.execute("""
        CREATE FUNCTION public.platform_saas_bot_timestamp_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        BEGIN
          IF TG_OP='INSERT' THEN
            NEW.created_at := clock_timestamp();
          ELSIF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'Bot connection timestamp is immutable'
              USING ERRCODE='42501';
          END IF;
          RETURN NEW;
        END;
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_trusted_bot_clock
        BEFORE INSERT OR UPDATE OF created_at ON public.bot_instances
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_bot_timestamp_guard()
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
          -- NEW.created_at is writable by the runtime role; never use it
          -- to decide enrollment. Only the DB clock and prior trusted
          -- connection events (excluding this NEW bot) may do so.
          IF effective_at IS NULL OR clock_timestamp()<effective_at
          THEN RETURN NEW; END IF;
          SELECT p.owner_user_id,p.merchant_id
            INTO recorded_owner,recorded_merchant
          FROM public.projects p WHERE p.id=NEW.project_id;
          IF recorded_owner IS NULL OR recorded_merchant IS NULL THEN
            RAISE EXCEPTION 'Verified project owner required'
              USING ERRCODE='42501';
          END IF;

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

          -- A platform operator can administer third-party channels.
          -- Do not consume their personal trial or infer creator proof.
          IF EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=recorded_owner AND o.role='owner'
          ) THEN RETURN NEW; END IF;

          -- Owner accounts with genuine bots before rollout are grandfathered.
          -- Do not include this row's potentially forged created_at/event;
          -- once claimed, that claim always takes priority over old events.
          IF started IS NULL THEN
            SELECT EXISTS(
              SELECT 1 FROM public.platform_usage_events ev
              JOIN public.merchant_project_periods mp
                ON mp.project_id=ev.project_id
               AND mp.owner_user_id=recorded_owner
              WHERE ev.event_type='bot_connected'
                AND ev.source_id<>NEW.id
                AND ev.occurred_at<effective_at
                AND mp.started_at<effective_at
                -- A recipient may have acquired an already connected bot:
                -- connection time precedes their owner period in that case.
                AND coalesce(mp.ended_at,'infinity'::timestamptz)
                    > greatest(ev.occurred_at,mp.started_at)
                AND NOT EXISTS(
                  SELECT 1 FROM public.platform_usage_events removed
                  WHERE removed.project_id=ev.project_id
                    AND removed.source_id=ev.source_id
                    AND removed.event_type='bot_removed'
                    AND removed.occurred_at
                        <= greatest(ev.occurred_at,mp.started_at)
                )
            ) INTO legacy_bot;
            IF legacy_bot THEN RETURN NEW; END IF;
          END IF;

          -- Even the FIRST trial claim must count existing bot rows.
          -- When access policy was enabled late or toggled OFF after its
          -- effective_from, an owner can already have an unclaimed bot.
          -- Exclude only the row currently being inserted, and reserve
          -- disabled/deleting rows until physical removal.
          IF started IS NULL
             OR clock_timestamp()<started+interval '72 hours' THEN
            SELECT EXISTS(
              SELECT 1 FROM public.bot_instances b
              JOIN public.projects p ON p.id=b.project_id
              WHERE p.owner_user_id=recorded_owner AND b.id<>NEW.id
            ) INTO other_connected;
            IF other_connected THEN
              RAISE EXCEPTION 'Only one bot is allowed during SaaS trial'
                USING ERRCODE='P6703';
            END IF;
          END IF;
          IF started IS NULL THEN
            -- A transaction-scoped owner lock and unique owner claim make
            -- the first enrolled bot atomic across projects and retries.
            INSERT INTO public.merchant_platform_trial_claims(
              id,merchant_id,owner_user_id,policy_key,scope,claimed_at
            ) VALUES (
              gen_random_uuid(),recorded_merchant,recorded_owner,
              'vimi-saas-72h-v1','owner',clock_timestamp()
            );
            RETURN NEW;
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

    # Ownership transfers update projects.owner_user_id, not bot_instances.
    # Enroll the recipient atomically with the existing consent/merchant
    # transfer transaction. Reusing the prior owner's Merchant ID as a trial
    # claim would fail the original initial_owner guard, so an owner-origin
    # identity is selected or created ONLY as the recipient's trial anchor.
    # Historical payments and project Merchant ID are never reassigned.
    op.execute("""
        CREATE FUNCTION public.platform_saas_transferred_bot_trial_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          effective_at timestamptz;
          recipient uuid;
          started timestamptz;
          new_merchant uuid;
          prior_bot boolean;
          recipient_bot_count integer;
        BEGIN
          IF NEW.owner_user_id IS NOT DISTINCT FROM OLD.owner_user_id
          THEN RETURN NEW; END IF;
          SELECT effective_from INTO effective_at
          FROM public.platform_saas_access_policy
          WHERE id=1 AND enabled=true;
          IF effective_at IS NULL OR clock_timestamp()<effective_at
          THEN RETURN NEW; END IF;
          recipient:=NEW.owner_user_id;
          IF NOT EXISTS(
            SELECT 1 FROM public.bot_instances b
            WHERE b.project_id=NEW.id AND b.status='registered'
          ) THEN RETURN NEW; END IF;
          PERFORM pg_advisory_xact_lock(
            hashtextextended('saas-trial-owner:'||recipient::text,0)
          );
          -- An operator-owned personal channel is exempt only if creator
          -- verified; admin-only transfers need separate manual review.
          IF EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=recipient AND o.role='owner'
          ) THEN RETURN NEW; END IF;
          SELECT claimed_at INTO started
          FROM public.merchant_platform_trial_claims
          WHERE owner_user_id=recipient AND scope='owner'
            AND policy_key='vimi-saas-72h-v1';
          IF started IS NULL THEN
            SELECT EXISTS(
              SELECT 1 FROM public.platform_usage_events ev
              JOIN public.merchant_project_periods mp
                ON mp.project_id=ev.project_id AND mp.owner_user_id=recipient
              WHERE ev.event_type='bot_connected'
                -- Historical owner periods in the incoming project
                -- count too. The just-created recipient period starts at
                -- transfer time, so the cutoff already excludes it.
                AND ev.occurred_at<effective_at
                AND mp.started_at<effective_at
                AND coalesce(mp.ended_at,'infinity'::timestamptz)
                    > greatest(ev.occurred_at,mp.started_at)
                AND NOT EXISTS(
                  SELECT 1 FROM public.platform_usage_events removed
                  WHERE removed.project_id=ev.project_id
                    AND removed.source_id=ev.source_id
                    AND removed.event_type='bot_removed'
                    AND removed.occurred_at
                        <= greatest(ev.occurred_at,mp.started_at)
                )
            ) INTO prior_bot;
            IF prior_bot THEN RETURN NEW; END IF;
          END IF;
          -- Count ALL persisted bots the recipient now owns, including
          -- additional rows in the incoming project itself. A disabled or
          -- retiring bot reserves a trial slot until physically removed.
          -- The AFTER UPDATE trigger sees NEW.owner_user_id on this project.
          SELECT count(*) INTO recipient_bot_count
          FROM public.bot_instances b
          JOIN public.projects p ON p.id=b.project_id
          WHERE p.owner_user_id=recipient;
          IF recipient_bot_count>1 AND (started IS NULL
              OR clock_timestamp()<started+interval '72 hours') THEN
            RAISE EXCEPTION 'Only one bot allowed during transferred trial'
              USING ERRCODE='P6703';
          END IF;
          IF started IS NULL THEN
            SELECT id INTO new_merchant
            FROM public.merchant_identities
            WHERE initial_owner_user_id=recipient
            ORDER BY created_at,id LIMIT 1;
            IF new_merchant IS NULL THEN
              new_merchant:=gen_random_uuid();
              INSERT INTO public.merchant_identities(id,initial_owner_user_id)
              VALUES(new_merchant,recipient);
            END IF;
            INSERT INTO public.merchant_platform_trial_claims(
              id,merchant_id,owner_user_id,policy_key,scope,claimed_at
            ) VALUES(
              gen_random_uuid(),new_merchant,recipient,
              'vimi-saas-72h-v1','owner',clock_timestamp()
            );
          END IF;
          RETURN NEW;
        END;
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_transferred_bot_trial
        AFTER UPDATE OF owner_user_id ON public.projects
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_transferred_bot_trial_guard()
    """)

    # Historic grandfathering is about actual bot-connected and verified
    # owner-period overlap, not the mere ABSENCE of a trial claim. During a
    # delayed enablement some owners had bots after effective_from while the
    # policy was OFF; they must still consume free-grant quota on activation.
    op.execute("""
        CREATE FUNCTION public.platform_saas_owner_was_legacy(
            p_owner uuid, p_effective timestamptz
        ) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $legacy$
          SELECT EXISTS(
            SELECT 1 FROM public.platform_usage_events ev
            JOIN public.merchant_project_periods mp
              ON mp.project_id=ev.project_id
             AND mp.owner_user_id=p_owner
            WHERE ev.event_type='bot_connected'
              AND ev.occurred_at<p_effective
              AND mp.started_at<p_effective
              AND coalesce(mp.ended_at,'infinity'::timestamptz)
                  > greatest(ev.occurred_at,mp.started_at)
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_usage_events gone
                WHERE gone.project_id=ev.project_id
                  AND gone.source_id=ev.source_id
                  AND gone.event_type='bot_removed'
                  AND gone.occurred_at
                      <= greatest(ev.occurred_at,mp.started_at)
              )
          )
        $legacy$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.platform_saas_owner_was_legacy("
        "uuid,timestamptz) FROM PUBLIC"
    )

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

    # Prevent a second source of free access via UPDATE instead of INSERT.
    # Grant identity, customer, resource, source, plan and validity are frozen.
    # Ordinary active->revoked transition still works; changes to payment
    # entitlements that remain payment-sourced are unaffected.
    op.execute("""
        CREATE FUNCTION public.platform_saas_manual_grant_update_guard()
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
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_manual_grant_update_guard
        BEFORE UPDATE ON public.entitlement_grants
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_manual_grant_update_guard()
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
        "platform_saas_bot_timestamp_guard()",
        "platform_saas_first_bot_trial_guard()",
        "platform_saas_transferred_bot_trial_guard()",
        "platform_saas_complimentary_grant_guard()",
        "platform_saas_manual_grant_update_guard()",
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
        ("entitlement_grants", "trg_saas_manual_grant_update_guard"),
        ("entitlement_grants", "trg_saas_complimentary_grant"),
        ("projects", "trg_saas_transferred_bot_trial"),
        ("bot_instances", "trg_saas_first_bot_trial"),
        ("bot_instances", "trg_saas_trusted_bot_clock"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON public.{table}")
    for fn in (
        "platform_saas_immutable_event_guard()",
        "platform_saas_manual_grant_update_guard()",
        "platform_saas_complimentary_grant_guard()",
        "platform_saas_transferred_bot_trial_guard()",
        "platform_saas_first_bot_trial_guard()",
        "platform_saas_bot_timestamp_guard()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{fn}")
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_owner_was_legacy("
        "uuid,timestamptz)"
    )
    op.drop_table("platform_saas_complimentary_events")
    op.drop_table("platform_saas_access_policy")
