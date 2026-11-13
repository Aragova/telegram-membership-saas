"""Expose real 72-hour trial state in the existing admin/billing screens.

Revision ID: 0076_owner_trial_view
Revises: 0075_saas_status

Read-only, current-actor scoped; no new timers, callbacks, charges or
activation. History is derived from immutable owner trial claims.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0076_owner_trial_view"
down_revision: str | None = "0075_saas_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.platform_saas_owner_trial_status()
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE
          actor uuid;
          now_at timestamptz:=transaction_timestamp();
          trial_started timestamptz;
          trial_ends timestamptz;
          effective_at timestamptz;
          enabled boolean;
          registered integer;
          exempt boolean;
          state text;
          seconds_left bigint;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF actor IS NULL THEN
            RAISE EXCEPTION 'Authenticated account required'
              USING ERRCODE='42501';
          END IF;
          SELECT EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators po
              ON po.telegram_user_id=u.telegram_user_id
            WHERE u.id=actor AND po.role='owner'
          ) INTO exempt;
          SELECT count(*) INTO registered
          FROM public.bot_instances b
          JOIN public.projects p ON p.id=b.project_id
          WHERE p.owner_user_id=actor AND b.status='registered';
          IF exempt THEN
            RETURN jsonb_build_object(
              'state','exempt','billing_enabled',false,
              'trial_started',NULL,'trial_ends_at',NULL,
              'remaining_seconds',NULL
            );
          END IF;
          SELECT b.enabled,b.effective_from INTO enabled,effective_at
          FROM public.platform_saas_billing_control b WHERE b.id=1;
          SELECT min(t.claimed_at) INTO trial_started
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=actor AND t.scope='owner'
            AND t.policy_key='vimi-saas-72h-v1';
          trial_ends:=trial_started+interval '72 hours';
          IF registered=0 THEN
            state:='not_connected';
          ELSIF enabled IS DISTINCT FROM true THEN
            state:='disabled';
          ELSIF effective_at IS NULL OR effective_at>now_at THEN
            state:='scheduled';
          ELSIF trial_started IS NULL THEN
            -- The supervisor may still be enrolling a connection made
            -- during the five-minute ARM delay. Never invent an end date.
            state:='pending';
          ELSIF trial_ends>now_at THEN
            state:='trial';
          ELSE
            state:='ended';
          END IF;
          seconds_left:=CASE WHEN trial_ends>now_at
            THEN floor(extract(epoch FROM trial_ends-now_at))::bigint
            ELSE 0 END;
          RETURN jsonb_build_object(
            'state',state,
            'billing_enabled',coalesce(enabled,false),
            'billing_effective_from',effective_at,
            'trial_started',trial_started,
            'trial_ends_at',trial_ends,
            'remaining_seconds',seconds_left
          );
        END; $fn$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_owner_trial_status() FROM PUBLIC"
    )
    op.execute("""
      DO $grant$ BEGIN
        IF EXISTS(
          SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
        ) THEN
          GRANT EXECUTE ON FUNCTION public.platform_saas_owner_trial_status()
          TO membership_runtime;
        END IF;
      END $grant$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.platform_saas_owner_trial_status()")
