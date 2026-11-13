"""Repair owner monetization activation status after creator-preflight removal.

Revision ID: 0075_saas_status
Revises: 0074_saas_refunds

Migration 0050 removed the obsolete creator-proof preflight RPC. Release status
in 0070 accidentally called it, making the existing operator's Monetization
button fail with a generic report error. Preserve its signature, permissions,
financial switches and current invoice history. No automatic activation.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0075_saas_status"
down_revision: str | None = "0074_saas_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_release_status()
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
      -- Migration 0050 dropped the creator-preflight RPC. All bots
      -- owned by the platform operator are already automatically exempt
      -- by authenticated owner account and historical project periods.
      -- No separate creator proof or pending review is required.
      pending_exempt:=0;
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


def downgrade() -> None:
    raise RuntimeError("Do not restore obsolete creator-preflight dependency")
