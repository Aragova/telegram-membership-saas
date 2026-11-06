"""Merchant-scoped commercial safety and owner-scoped bot limits.

Revision ID: 0029_merchant_commercial_guards
Revises: 0028_merchant_financial_report

No pricing, trial offer, quota or commission is automatically activated.
Trusted operator configures an explicit limit/policy before it can apply.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_merchant_commercial_guards"
down_revision: str | None = "0028_merchant_financial_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No row means unlimited. Caps are across *all* projects of the same
    # verified Telegram account, so a new project / Merchant ID cannot reset
    # the same owner's configured connected-bot quota.
    op.create_table(
        "platform_owner_bot_limits",
        sa.Column(
            "owner_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("max_connected_bots", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.CheckConstraint(
            "max_connected_bots BETWEEN 1 AND 10000",
            name="ck_platform_owner_bot_limit",
        ),
    )

    # Trial offers are separate from *buyer* entitlement_grants.source=trial.
    # Future operator policy explicitly chooses once-per-business or
    # once-per-owner. Neither scope is inferred from chat administrators.
    op.create_table(
        "merchant_platform_trial_claims",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "merchant_id", sa.Uuid(),
            sa.ForeignKey("merchant_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("policy_key", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column(
            "claimed_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('merchant', 'owner')", name="ck_merchant_trial_scope",
        ),
        sa.CheckConstraint(
            "length(btrim(policy_key)) > 0", name="ck_merchant_trial_policy",
        ),
    )
    op.create_index(
        "uq_merchant_trial_per_merchant",
        "merchant_platform_trial_claims",
        ["policy_key", "merchant_id"], unique=True,
        postgresql_where=sa.text("scope = 'merchant'"),
    )
    op.create_index(
        "uq_merchant_trial_per_owner",
        "merchant_platform_trial_claims",
        ["policy_key", "owner_user_id"], unique=True,
        postgresql_where=sa.text("scope = 'owner'"),
    )
    op.create_index(
        "ix_merchant_trial_owner",
        "merchant_platform_trial_claims", ["owner_user_id", "claimed_at"],
    )

    # Reservations are NOT charges or invoices. One canonical Payment UUID
    # can be claimed once for any future verified per-payment commission.
    # Original ledger and historical merchant attribution must agree.
    op.create_table(
        "merchant_commission_reservations",
        sa.Column("payment_source_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "usage_event_id", sa.BigInteger(),
            sa.ForeignKey("platform_usage_events.id", ondelete="RESTRICT"),
            unique=True, nullable=False,
        ),
        sa.Column(
            "merchant_id", sa.Uuid(),
            sa.ForeignKey("merchant_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("policy_key", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(policy_key)) > 0", name="ck_merchant_commission_policy",
        ),
    )
    op.create_index(
        "ix_merchant_commission_merchant",
        "merchant_commission_reservations", ["merchant_id", "created_at"],
    )

    # Signals are for human investigation only: a channel can legally
    # transfer to a different owner. Do not reveal or auto-merge merchants.
    op.create_table(
        "merchant_risk_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "merchant_id", sa.Uuid(),
            sa.ForeignKey("merchant_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_chat_id", sa.String(32), nullable=False),
        sa.Column("risk_kind", sa.String(48), nullable=False),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"), nullable=False,
        ),
        sa.UniqueConstraint(
            "merchant_id", "project_id", "telegram_chat_id", "risk_kind",
            name="uq_merchant_risk_observation",
        ),
        sa.CheckConstraint(
            "risk_kind = 'historical_chat_other_owner'",
            name="ck_merchant_risk_kind",
        ),
    )
    op.create_index(
        "ix_merchant_risk_merchant",
        "merchant_risk_observations", ["merchant_id", "observed_at"],
    )

    for table in (
        "platform_owner_bot_limits", "merchant_platform_trial_claims",
        "merchant_commission_reservations", "merchant_risk_observations",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        # No tenant SELECT / INSERT / UPDATE / DELETE policies.
    # The DB uses ALTER DEFAULT PRIVILEGES to grant future table CRUD to
    # membership_runtime; revoke it explicitly before runtime can invoke a
    # privileged BEFORE INSERT trigger as an existence oracle.
    op.execute("""
        DO $revoke$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
            REVOKE ALL ON
              public.platform_owner_bot_limits,
              public.merchant_platform_trial_claims,
              public.merchant_commission_reservations,
              public.merchant_risk_observations
            FROM membership_runtime;
          END IF;
        END
        $revoke$
    """)

    op.execute("""
        CREATE FUNCTION public.merchant_commercial_claim_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
            source_owner uuid;
            event_project uuid;
            event_time timestamptz;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'Commercial claims cannot be changed or erased'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_TABLE_NAME = 'merchant_platform_trial_claims' THEN
                SELECT initial_owner_user_id INTO source_owner
                FROM public.merchant_identities
                WHERE id = NEW.merchant_id;
                IF source_owner IS DISTINCT FROM NEW.owner_user_id THEN
                    RAISE EXCEPTION 'Trial claim owner does not match merchant'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'merchant_commission_reservations' THEN
                SELECT e.project_id, e.occurred_at
                INTO event_project, event_time
                FROM public.platform_usage_events e
                WHERE e.id = NEW.usage_event_id
                  AND e.event_type = 'payment_approved'
                  AND e.source_id = NEW.payment_source_id
                  -- Only the globally earliest immutable event is canonical.
                  AND e.id = (
                      SELECT min(other.id)
                      FROM public.platform_usage_events other
                      WHERE other.event_type = 'payment_approved'
                        AND other.source_id = NEW.payment_source_id
                  );
                IF event_project IS NULL OR NOT EXISTS (
                    SELECT 1 FROM public.merchant_project_periods p
                    WHERE p.project_id = event_project
                      AND p.merchant_id = NEW.merchant_id
                      AND p.started_at <= event_time
                      AND (p.ended_at IS NULL OR event_time <= p.ended_at)
                ) THEN
                    RAISE EXCEPTION 'Commission source/merchant mismatch'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'Unexpected commercial claim table'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $guard$
    """)
    for table in (
        "merchant_platform_trial_claims", "merchant_commission_reservations",
    ):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_claim_guard
            BEFORE INSERT OR UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION public.merchant_commercial_claim_guard()
        """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.merchant_commercial_claim_guard() FROM PUBLIC"
    )

    op.execute("""
        CREATE FUNCTION public.merchant_owner_bot_limit_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $quota$
        DECLARE
            owner_id uuid;
            actor uuid;
            selected_project uuid;
            cap integer;
            connected integer;
        BEGIN
            -- Count registered and disabled bots as connected; only deleting
            -- an actual connection frees a slot. A retry/reenable is not a
            -- second registration. No quota exists until explicitly set.
            IF NEW.status NOT IN ('registered', 'disabled') THEN
                RETURN NEW;
            END IF;
            actor := NULLIF(current_setting(
                'app.current_user_id', true), '')::uuid;
            selected_project := NULLIF(current_setting(
                'app.current_project_id', true), '')::uuid;

            SELECT p.owner_user_id INTO owner_id
              FROM public.projects p WHERE p.id = NEW.project_id;
            -- Only trust tenant identity on real runtime connections, before
            -- privileged cross-project quota reads. Migration DB owner can
            -- seed fixtures/perform explicit recovery without GUC context.
            IF SESSION_USER <> CURRENT_USER
               AND (actor IS NULL OR actor IS DISTINCT FROM owner_id
                    OR selected_project IS DISTINCT FROM NEW.project_id) THEN
                RAISE EXCEPTION 'Bot project scope denied'
                    USING ERRCODE = '42501';
            END IF;

            PERFORM pg_advisory_xact_lock(
                hashtextextended('vimi:owner-cap:' || owner_id::text, 0)
            );
            SELECT l.max_connected_bots INTO cap
            FROM public.platform_owner_bot_limits l
            WHERE l.owner_user_id = owner_id;
            IF cap IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT count(*) INTO connected
            FROM public.bot_instances bot
            JOIN public.projects p ON p.id = bot.project_id
            WHERE p.owner_user_id = owner_id
              AND bot.status IN ('registered', 'disabled');
            IF connected >= cap THEN
                RAISE EXCEPTION 'Configured connected bot limit reached'
                    USING ERRCODE = 'P6701';
            END IF;
            RETURN NEW;
        END
        $quota$
    """)
    op.execute("""
        CREATE TRIGGER trg_merchant_owner_bot_limit
        BEFORE INSERT ON public.bot_instances
        FOR EACH ROW EXECUTE FUNCTION public.merchant_owner_bot_limit_guard()
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.merchant_owner_bot_limit_guard() FROM PUBLIC"
    )

    # Keep commercially used channel history even after project purge.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.merchant_guard_channel_reuse()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $merchant$
        DECLARE
            current_owner uuid;
            caller_user uuid;
            caller_project uuid;
        BEGIN
            -- A disabled channel cannot be sold; check again on activation.
            IF NEW.status <> 'active'
               OR NEW.external_id !~ '^-?[1-9][0-9]{0,18}
    op.execute("DROP FUNCTION IF EXISTS public.merchant_detect_channel_transfer_risk()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_merchant_owner_bot_limit ON public.bot_instances"
    )
    op.execute("DROP FUNCTION IF EXISTS public.merchant_owner_bot_limit_guard()")
    for table in (
        "merchant_commission_reservations", "merchant_platform_trial_claims",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_claim_guard ON public.{table}")
    op.execute("DROP FUNCTION IF EXISTS public.merchant_commercial_claim_guard()")
    for table in (
        "merchant_risk_observations", "merchant_commission_reservations",
        "merchant_platform_trial_claims", "platform_owner_bot_limits",
    ):
        op.drop_table(table)
 THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND NEW.project_id IS NOT DISTINCT FROM OLD.project_id
               AND NEW.external_id IS NOT DISTINCT FROM OLD.external_id
               AND NEW.resource_type IS NOT DISTINCT FROM OLD.resource_type
               AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;

            -- Authenticate before examining any cross-tenant history.
            caller_user := NULLIF(current_setting(
                'app.current_user_id', true), '')::uuid;
            caller_project := NULLIF(current_setting(
                'app.current_project_id', true), '')::uuid;
            IF caller_user IS NULL OR caller_project IS DISTINCT FROM NEW.project_id THEN
                RAISE EXCEPTION 'Resource project scope denied'
                    USING ERRCODE = '42501';
            END IF;
            SELECT p.owner_user_id INTO current_owner
            FROM public.projects p
            WHERE p.id = NEW.project_id AND p.status = 'active'
              AND (
                  p.owner_user_id = caller_user
                  OR EXISTS (
                      SELECT 1 FROM public.project_members pm
                      WHERE pm.project_id = p.id AND pm.user_id = caller_user
                        AND pm.role = 'admin'
                  )
              );
            IF current_owner IS NULL THEN
                RAISE EXCEPTION 'Resource project scope denied'
                    USING ERRCODE = '42501';
            END IF;

            PERFORM pg_advisory_xact_lock(
                hashtextextended('vimi:channel:' || NEW.external_id, 0)
            );

            IF EXISTS (
                SELECT 1
                FROM public.merchant_channel_periods c
                JOIN public.projects previous
                  ON previous.id = c.project_id
                WHERE c.telegram_chat_id = NEW.external_id
                  AND c.project_id <> NEW.project_id
                  AND previous.owner_user_id = current_owner
            ) THEN
                RAISE EXCEPTION
                    'Channel belongs to another existing connection of this owner'
                    USING ERRCODE = 'P6501';
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.merchant_channel_periods c
                JOIN public.merchant_project_periods prev
                  ON prev.project_id = c.project_id
                 AND prev.merchant_id = c.merchant_id
                WHERE c.telegram_chat_id = NEW.external_id
                  AND c.project_id <> NEW.project_id
                  AND prev.owner_user_id = current_owner
                  AND NOT EXISTS (
                      SELECT 1 FROM public.projects p
                      WHERE p.id = c.project_id
                  )
                  AND (
                      EXISTS (
                          SELECT 1 FROM public.platform_usage_events e
                          WHERE e.project_id = c.project_id
                            AND e.event_type = 'payment_approved'
                      )
                      OR EXISTS (
                          SELECT 1 FROM public.merchant_platform_trial_claims trial
                          WHERE trial.merchant_id = c.merchant_id
                      )
                      OR EXISTS (
                          SELECT 1 FROM public.merchant_commission_reservations reserve
                          WHERE reserve.merchant_id = c.merchant_id
                      )
                  )
            ) THEN
                RAISE EXCEPTION
                    'Channel has historical commercial use in a purged connection'
                    USING ERRCODE = 'P6702';
            END IF;
            RETURN NEW;
        END
        $merchant$
    """)

    op.execute("""
        CREATE FUNCTION public.merchant_detect_channel_transfer_risk()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $risk$
        DECLARE
            target_owner uuid;
            target_merchant uuid;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.status IS NOT DISTINCT FROM OLD.status
                   AND NEW.external_id IS NOT DISTINCT FROM OLD.external_id
                   AND NEW.resource_type IS NOT DISTINCT FROM OLD.resource_type
                   AND NEW.project_id IS NOT DISTINCT FROM OLD.project_id THEN
                    RETURN NEW;
                END IF;
            END IF;
            IF NEW.status <> 'active'
               OR NEW.resource_type NOT IN (
                   'telegram_channel', 'telegram_group'
               )
               OR NEW.external_id !~ '^-?[1-9][0-9]{0,18}
            INTO target_owner, target_merchant
            FROM public.projects p WHERE p.id = NEW.project_id;
            IF target_owner IS NULL THEN RETURN NEW; END IF;
            -- A historical observation is NOT legal proof of ownership.
            -- Record only an operator-visible review signal, never block a
            -- verified legitimate transfer or reveal the old owner's data.
            IF EXISTS (
                SELECT 1 FROM public.merchant_channel_periods c
                JOIN public.merchant_project_periods mp
                  ON mp.project_id = c.project_id
                 AND mp.merchant_id = c.merchant_id
                WHERE c.telegram_chat_id = NEW.external_id
                  AND c.project_id <> NEW.project_id
                  AND mp.owner_user_id <> target_owner
            ) THEN
                INSERT INTO public.merchant_risk_observations (
                    id, merchant_id, project_id, telegram_chat_id, risk_kind
                ) VALUES (
                    gen_random_uuid(), target_merchant, NEW.project_id,
                    NEW.external_id, 'historical_chat_other_owner'
                ) ON CONFLICT (
                    merchant_id, project_id, telegram_chat_id, risk_kind
                ) DO NOTHING;
            END IF;
            RETURN NEW;
        END
        $risk$
    """)
    op.execute("""
        CREATE TRIGGER trg_merchant_channel_transfer_risk
        AFTER INSERT OR UPDATE ON public.resources
        FOR EACH ROW EXECUTE FUNCTION public.merchant_detect_channel_transfer_risk()
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.merchant_detect_channel_transfer_risk() FROM PUBLIC
    """)



def downgrade() -> None:
    # Restore the PR65 channel guard before removing PR67 commercial tables.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.merchant_guard_channel_reuse()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $merchant$
        DECLARE
            current_owner uuid;
            caller_user uuid;
            caller_project uuid;
        BEGIN
            -- A disabled channel cannot be sold; check again on activation.
            IF NEW.status <> 'active'
               OR NEW.external_id !~ '^-?[1-9][0-9]{0,18}$' THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND NEW.project_id IS NOT DISTINCT FROM OLD.project_id
               AND NEW.external_id IS NOT DISTINCT FROM OLD.external_id
               AND NEW.resource_type IS NOT DISTINCT FROM OLD.resource_type
               AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;

            -- Triggers run before INSERT RLS WITH CHECK. A SECURITY DEFINER
            -- function MUST authenticate caller scope before looking at
            -- private historical channel links (otherwise a forged target
            -- project UUID creates a cross-tenant existence oracle).
            caller_user := NULLIF(current_setting(
                'app.current_user_id', true), '')::uuid;
            caller_project := NULLIF(current_setting(
                'app.current_project_id', true), '')::uuid;
            IF caller_user IS NULL OR caller_project IS DISTINCT FROM NEW.project_id THEN
                RAISE EXCEPTION 'Resource project scope denied'
                  USING ERRCODE = '42501';
            END IF;
            SELECT p.owner_user_id INTO current_owner
              FROM public.projects p
             WHERE p.id = NEW.project_id
               AND p.status = 'active'
               AND (
                   p.owner_user_id = caller_user
                   OR EXISTS (
                       SELECT 1 FROM public.project_members pm
                        WHERE pm.project_id = p.id
                          AND pm.user_id = caller_user
                          AND pm.role = 'admin'
                   )
               );
            IF current_owner IS NULL THEN
                RAISE EXCEPTION 'Resource project scope denied'
                  USING ERRCODE = '42501';
            END IF;

            -- Serialize even concurrent channel attachments in two projects:
            -- the second transaction must see the first committed observation.
            PERFORM pg_advisory_xact_lock(
                hashtextextended('vimi:channel:' || NEW.external_id, 0)
            );

            IF EXISTS (
                SELECT 1
                FROM public.merchant_channel_periods observation
                JOIN public.projects previous
                  ON previous.id = observation.project_id
                WHERE observation.telegram_chat_id = NEW.external_id
                  AND observation.project_id <> NEW.project_id
                  AND previous.owner_user_id = current_owner
            ) THEN
                RAISE EXCEPTION
                  'Channel belongs to another existing connection of this owner'
                  USING ERRCODE = 'P6501';
            END IF;

            RETURN NEW;
        END
        $merchant$
    """)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_merchant_channel_transfer_risk ON public.resources"
    )
    op.execute("DROP FUNCTION IF EXISTS public.merchant_detect_channel_transfer_risk()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_merchant_owner_bot_limit ON public.bot_instances"
    )
    op.execute("DROP FUNCTION IF EXISTS public.merchant_owner_bot_limit_guard()")
    for table in (
        "merchant_commission_reservations", "merchant_platform_trial_claims",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_claim_guard ON public.{table}")
    op.execute("DROP FUNCTION IF EXISTS public.merchant_commercial_claim_guard()")
    for table in (
        "merchant_risk_observations", "merchant_commission_reservations",
        "merchant_platform_trial_claims", "platform_owner_bot_limits",
    ):
        op.drop_table(table)
 THEN
                RETURN NEW;
            END IF;

            SELECT p.owner_user_id, p.merchant_id
            INTO target_owner, target_merchant
            FROM public.projects p WHERE p.id = NEW.project_id;
            IF target_owner IS NULL THEN RETURN NEW; END IF;
            -- A historical observation is NOT legal proof of ownership.
            -- Record only an operator-visible review signal, never block a
            -- verified legitimate transfer or reveal the old owner's data.
            IF EXISTS (
                SELECT 1 FROM public.merchant_channel_periods c
                JOIN public.merchant_project_periods mp
                  ON mp.project_id = c.project_id
                 AND mp.merchant_id = c.merchant_id
                WHERE c.telegram_chat_id = NEW.external_id
                  AND c.project_id <> NEW.project_id
                  AND mp.owner_user_id <> target_owner
            ) THEN
                INSERT INTO public.merchant_risk_observations (
                    id, merchant_id, project_id, telegram_chat_id, risk_kind
                ) VALUES (
                    gen_random_uuid(), target_merchant, NEW.project_id,
                    NEW.external_id, 'historical_chat_other_owner'
                ) ON CONFLICT (
                    merchant_id, project_id, telegram_chat_id, risk_kind
                ) DO NOTHING;
            END IF;
            RETURN NEW;
        END
        $risk$
    """)
    op.execute("""
        CREATE TRIGGER trg_merchant_channel_transfer_risk
        AFTER INSERT ON public.resources
        FOR EACH ROW EXECUTE FUNCTION public.merchant_detect_channel_transfer_risk()
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.merchant_detect_channel_transfer_risk() FROM PUBLIC
    """)



def downgrade() -> None:
    # Restore the PR65 channel guard before removing PR67 commercial tables.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.merchant_guard_channel_reuse()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $merchant$
        DECLARE
            current_owner uuid;
            caller_user uuid;
            caller_project uuid;
        BEGIN
            -- A disabled channel cannot be sold; check again on activation.
            IF NEW.status <> 'active'
               OR NEW.external_id !~ '^-?[1-9][0-9]{0,18}$' THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND NEW.project_id IS NOT DISTINCT FROM OLD.project_id
               AND NEW.external_id IS NOT DISTINCT FROM OLD.external_id
               AND NEW.resource_type IS NOT DISTINCT FROM OLD.resource_type
               AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;

            -- Triggers run before INSERT RLS WITH CHECK. A SECURITY DEFINER
            -- function MUST authenticate caller scope before looking at
            -- private historical channel links (otherwise a forged target
            -- project UUID creates a cross-tenant existence oracle).
            caller_user := NULLIF(current_setting(
                'app.current_user_id', true), '')::uuid;
            caller_project := NULLIF(current_setting(
                'app.current_project_id', true), '')::uuid;
            IF caller_user IS NULL OR caller_project IS DISTINCT FROM NEW.project_id THEN
                RAISE EXCEPTION 'Resource project scope denied'
                  USING ERRCODE = '42501';
            END IF;
            SELECT p.owner_user_id INTO current_owner
              FROM public.projects p
             WHERE p.id = NEW.project_id
               AND p.status = 'active'
               AND (
                   p.owner_user_id = caller_user
                   OR EXISTS (
                       SELECT 1 FROM public.project_members pm
                        WHERE pm.project_id = p.id
                          AND pm.user_id = caller_user
                          AND pm.role = 'admin'
                   )
               );
            IF current_owner IS NULL THEN
                RAISE EXCEPTION 'Resource project scope denied'
                  USING ERRCODE = '42501';
            END IF;

            -- Serialize even concurrent channel attachments in two projects:
            -- the second transaction must see the first committed observation.
            PERFORM pg_advisory_xact_lock(
                hashtextextended('vimi:channel:' || NEW.external_id, 0)
            );

            IF EXISTS (
                SELECT 1
                FROM public.merchant_channel_periods observation
                JOIN public.projects previous
                  ON previous.id = observation.project_id
                WHERE observation.telegram_chat_id = NEW.external_id
                  AND observation.project_id <> NEW.project_id
                  AND previous.owner_user_id = current_owner
            ) THEN
                RAISE EXCEPTION
                  'Channel belongs to another existing connection of this owner'
                  USING ERRCODE = 'P6501';
            END IF;

            RETURN NEW;
        END
        $merchant$
    """)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_merchant_channel_transfer_risk ON public.resources"
    )
    op.execute("DROP FUNCTION IF EXISTS public.merchant_detect_channel_transfer_risk()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_merchant_owner_bot_limit ON public.bot_instances"
    )
    op.execute("DROP FUNCTION IF EXISTS public.merchant_owner_bot_limit_guard()")
    for table in (
        "merchant_commission_reservations", "merchant_platform_trial_claims",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_claim_guard ON public.{table}")
    op.execute("DROP FUNCTION IF EXISTS public.merchant_commercial_claim_guard()")
    for table in (
        "merchant_risk_observations", "merchant_commission_reservations",
        "merchant_platform_trial_claims", "platform_owner_bot_limits",
    ):
        op.drop_table(table)
