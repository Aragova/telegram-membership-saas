"""Owner-confirmed commercial pilot switch with audit; NOT auto-activated.

Revision ID: 0070_saas_pilot
Revises: 0069_saas_balance

No migration-time billing changes. Each real switch requires an authenticated
platform owner, fresh bot/account preflight and explicit two-step Telegram UI.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070_saas_pilot"
down_revision: str | None = "0069_saas_balance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_saas_pilot_audit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(12), nullable=False),
        sa.Column("registered_bots", sa.Integer(), nullable=False),
        sa.Column("nonoperator_owners", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint(
            "action IN ('arm','stop','holdon','holdoff')",
            name="ck_saas_pilot_action",
        ),
    )
    op.execute(
        "ALTER TABLE public.platform_saas_pilot_audit "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_pilot_audit "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
      CREATE TRIGGER trg_saas_pilot_immutable
      BEFORE UPDATE OR DELETE ON public.platform_saas_pilot_audit
      FOR EACH ROW EXECUTE FUNCTION
      public.platform_saas_credit_entry_immutable()
    """)

    op.execute("""
    CREATE FUNCTION public.platform_saas_pilot_status()
    RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE
      total_bots integer;
      other_owners integer;
      operator_bots integer;
      other_bots integer;
      billing record;
      trial record;
      holds record;
      pending_exempt integer;
      open_invoices bigint;
      result jsonb;
    BEGIN
      PERFORM public.platform_saas_require_owner();
      SELECT count(*)::integer,
             count(*) FILTER(WHERE po.telegram_user_id IS NOT NULL)::integer,
             count(*) FILTER(WHERE po.telegram_user_id IS NULL)::integer,
             count(DISTINCT p.owner_user_id) FILTER(
               WHERE po.telegram_user_id IS NULL
             )::integer
        INTO total_bots,operator_bots,other_bots,other_owners
      FROM public.bot_instances b
      JOIN public.projects p ON p.id=b.project_id
      JOIN public.telegram_users u ON u.id=p.owner_user_id
      LEFT JOIN public.platform_operators po
        ON po.telegram_user_id=u.telegram_user_id AND po.role='owner'
      WHERE b.status='registered';
      SELECT * INTO billing FROM public.platform_saas_billing_control
      WHERE id=1;
      SELECT * INTO trial FROM public.platform_saas_access_policy
      WHERE id=1;
      SELECT * INTO holds FROM public.platform_saas_hold_control
      WHERE id=1;
      SELECT count(*) INTO open_invoices
      FROM public.platform_saas_invoices i
      WHERE i.status='issued' AND public.platform_saas_v2_remaining(i.id)>0;
      SELECT coalesce((public.platform_saas_exemption_preflight()
                       ->>'pending')::integer,0) INTO pending_exempt;
      result:=jsonb_build_object(
        'billing_enabled',coalesce(billing.enabled,false),
        'billing_from',billing.effective_from,
        'trial_enabled',coalesce(trial.enabled,false),
        'trial_from',trial.effective_from,
        'holds_enabled',coalesce(holds.enabled,false),
        'holds_from',holds.effective_from,
        'grace_days',holds.grace_days,
        'registered_bots',total_bots,
        'operator_bots',operator_bots,
        'nonoperator_bots',other_bots,
        'nonoperator_owners',other_owners,
        'operator_exemption_pending',pending_exempt,
        'open_invoices',open_invoices,
        'automatic_invoice_issuance',false,
        'real_bank_transfers_automated',false
      );
      RETURN result;
    END; $f$
    """)

    op.execute("""
    CREATE FUNCTION public.platform_saas_pilot_change(
      p_action text, p_expected_bots integer,
      p_expected_other_owners integer
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE
      actor uuid;
      before_state jsonb;
      start_at timestamptz;
      old_billing boolean;
      old_holds boolean;
    BEGIN
      actor:=public.platform_saas_require_owner();
      IF p_action NOT IN ('arm','stop','holdon','holdoff') THEN
        RAISE EXCEPTION 'Invalid SaaS pilot action' USING ERRCODE='22023';
      END IF;
      PERFORM pg_advisory_xact_lock(
        hashtextextended('vimi:saas:pilot-control',0)
      );
      before_state:=public.platform_saas_pilot_status();
      IF p_action IN ('arm','holdon') AND (
           p_expected_bots IS NULL OR p_expected_other_owners IS NULL
           OR p_expected_bots<>(
             before_state->>'registered_bots'
           )::integer
           OR p_expected_other_owners<>(
             before_state->>'nonoperator_owners'
           )::integer
         ) THEN
        RAISE EXCEPTION 'Pilot bot/account inventory changed, review again'
          USING ERRCODE='23514';
      END IF;
      SELECT enabled INTO old_billing
      FROM public.platform_saas_billing_control WHERE id=1;
      SELECT enabled INTO old_holds
      FROM public.platform_saas_hold_control WHERE id=1;
      IF p_action='arm' THEN
        IF old_billing THEN
          RAISE EXCEPTION 'Commercial pilot already active'
            USING ERRCODE='23514';
        END IF;
        -- No backdated bills, no quota/charge before this DB-clock cutoff.
        start_at:=clock_timestamp()+interval '5 minutes';
        UPDATE public.platform_saas_billing_control
        SET enabled=true,effective_from=start_at WHERE id=1;
        UPDATE public.platform_saas_access_policy
        SET enabled=true,effective_from=start_at WHERE id=1;
        UPDATE public.platform_saas_hold_control
        SET enabled=false,effective_from=NULL WHERE id=1;
      ELSIF p_action='holdon' THEN
        IF old_billing IS DISTINCT FROM true OR old_holds IS TRUE THEN
          RAISE EXCEPTION 'Activate billing before hold pilot'
            USING ERRCODE='42501';
        END IF;
        start_at:=clock_timestamp()+interval '5 minutes';
        UPDATE public.platform_saas_hold_control
        SET enabled=true,effective_from=start_at,grace_days=7 WHERE id=1;
      ELSIF p_action='holdoff' THEN
        UPDATE public.platform_saas_hold_control
        SET enabled=false,effective_from=NULL WHERE id=1;
      ELSE
        -- Emergency off: no new service charges, trials or holds.
        -- Existing invoice evidence and already bought rights survive.
        UPDATE public.platform_saas_hold_control
        SET enabled=false,effective_from=NULL WHERE id=1;
        UPDATE public.platform_saas_access_policy
        SET enabled=false,effective_from=NULL WHERE id=1;
        UPDATE public.platform_saas_billing_control
        SET enabled=false,effective_from=NULL WHERE id=1;
      END IF;
      INSERT INTO public.platform_saas_pilot_audit(
        id,actor_id,action,registered_bots,nonoperator_owners,effective_from
      ) VALUES (
        gen_random_uuid(),actor,p_action,
        (before_state->>'registered_bots')::integer,
        (before_state->>'nonoperator_owners')::integer,start_at
      );
      RETURN public.platform_saas_pilot_status();
    END; $f$
    """)
    for signature in (
        "platform_saas_pilot_status()",
        "platform_saas_pilot_change(text,integer,integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"""
        DO $grant$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION public.{signature}
            TO membership_runtime;
          END IF;
        END $grant$
        """)
    op.execute("""
       DO $revoke$
       BEGIN
         IF EXISTS(
           SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
         ) THEN
           REVOKE ALL ON public.platform_saas_pilot_audit
           FROM membership_runtime;
         END IF;
       END $revoke$
    """)


def downgrade() -> None:
    raise RuntimeError("Never roll back audited ViMi Pay pilot switch history")
