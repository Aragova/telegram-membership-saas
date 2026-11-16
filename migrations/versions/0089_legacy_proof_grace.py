"""Allow pending original bank proof to protect one grandfathered bot.

Revision ID: 0089_legacy_proof_grace
Revises: 0088_backup_monitor

A pre-activation issued invoice can be held for that bot but is intentionally
ignored by owner-wide commercial debt. Grant the existing first-proof 12-hour
review respite ONLY for that one bot. Never retroactively charge/hold the
owner's other bots and never edit existing issued financial records.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0089_legacy_proof_grace"
down_revision: str | None = "0088_backup_monitor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(r"""CREATE OR REPLACE FUNCTION public.platform_saas_bot_hold_state(
  p_bot uuid,p_at timestamptz
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  owner_id uuid;
  merchant_id uuid;
  project_id uuid;
  started timestamptz;
  billing_started timestamptz;
  enabled boolean;
  is_unpaid boolean;
  owner_network text;
  last_covered timestamptz;
BEGIN
  IF p_bot IS NULL OR p_at IS NULL THEN
    RAISE EXCEPTION 'Invalid bot hold check' USING ERRCODE='22023';
  END IF;
  SELECT p.owner_user_id,p.merchant_id,b.project_id
    INTO owner_id,merchant_id,project_id
  FROM public.bot_instances b JOIN public.projects p ON p.id=b.project_id
  WHERE b.id=p_bot AND b.status='registered';
  IF owner_id IS NULL THEN RETURN 'disabled'; END IF;
  SELECT h.enabled,h.effective_from INTO enabled,started
  FROM public.platform_saas_hold_control h WHERE h.id=1;
  SELECT c.effective_from INTO billing_started
  FROM public.platform_saas_billing_control c WHERE c.id=1 AND c.enabled;
  IF NOT coalesce(enabled,false) OR started IS NULL
     OR billing_started IS NULL
     OR p_at<greatest(started,billing_started) THEN RETURN 'disabled'; END IF;
  -- Platform staff may administrate someone else's paid channel.
  -- An operator role alone is NOT proof of a creator-owned exemption.
  IF public.platform_saas_exemption_state(
    project_id,p_bot,merchant_id,owner_id,p_at
  )='exempt' THEN RETURN 'clear'; END IF;
  -- An overdue invoice belongs to the liable OWNER. Each nonexempt bot
  -- must enforce the same network-level debt, not only its own service fee.
  owner_network:=public.platform_saas_owner_overdue_state(owner_id,p_at);
  IF owner_network='held' THEN RETURN 'held'; END IF;
  -- A genuinely pending first bank proof holds sanctions for at most
  -- twelve hours, including a previously expired own service term.
  IF owner_network='review' THEN RETURN 'clear'; END IF;

  -- A historical invoice issued BEFORE commercial activation is deliberately
  -- ignored by owner-wide overdue enforcement. However, the existing bot
  -- can still be held for that same issued unpaid bill. Recognize ONE genuine
  -- pending first proof for this BOT only, within its immutable 12h window.
  -- Do not turn old invoices into new owner-network liabilities.
  IF owner_network='clear' AND EXISTS(
    SELECT 1 FROM public.platform_saas_invoices i
    WHERE i.bot_instance_id=p_bot AND i.owner_user_id=owner_id
      AND i.kind='prepayment' AND i.status='issued'
      AND i.issued_at IS NOT NULL AND i.issued_at<billing_started
      AND i.due_at<=p_at AND public.platform_saas_v2_remaining(i.id)>0
      AND (
        SELECT min(p.submitted_at)
        FROM public.platform_saas_payment_proofs_v2 p
        JOIN public.platform_saas_payment_attempts_v2 a
          ON a.id=p.attempt_id
        WHERE a.invoice_id=i.id
      ) > p_at-interval '12 hours'
      AND EXISTS(
        SELECT 1 FROM public.platform_saas_payment_proofs_v2 p
        JOIN public.platform_saas_payment_attempts_v2 a
          ON a.id=p.attempt_id
        WHERE a.invoice_id=i.id
          AND NOT EXISTS(
            SELECT 1 FROM public.platform_saas_payment_allocations_v2 ok
            WHERE ok.attempt_id=a.id
          )
          AND NOT EXISTS(
            SELECT 1 FROM public.platform_saas_payment_rejections_v2 bad
            WHERE bad.attempt_id=a.id
          )
      )
      AND NOT EXISTS(
        SELECT 1 FROM public.platform_saas_payment_attempts_v2 a
        WHERE a.invoice_id=i.id
          AND (
            EXISTS(SELECT 1 FROM public.platform_saas_payment_allocations_v2 ok
                   WHERE ok.attempt_id=a.id)
            OR EXISTS(SELECT 1 FROM public.platform_saas_payment_rejections_v2 bad
                      WHERE bad.attempt_id=a.id)
          )
      )
      AND NOT EXISTS(
        SELECT 1 FROM public.platform_saas_invoices other
        WHERE other.bot_instance_id=p_bot
          AND other.owner_user_id=owner_id
          AND other.id<>i.id
          AND other.kind='prepayment' AND other.status='issued'
          AND other.due_at<=p_at
          AND public.platform_saas_v2_remaining(other.id)>0
      )
  ) THEN RETURN 'clear'; END IF;

  SELECT EXISTS(
    SELECT 1 FROM public.platform_saas_invoices i
    WHERE i.bot_instance_id=p_bot AND i.owner_user_id=owner_id
      AND i.kind='prepayment' AND i.status='issued'
      AND i.due_at<=p_at AND public.platform_saas_v2_remaining(i.id)>0
  ) INTO is_unpaid;
  IF is_unpaid THEN RETURN 'held'; END IF;
  SELECT max(coalesce(a.effective_end,i.period_end)) INTO last_covered
  FROM public.platform_saas_invoices i
  LEFT JOIN public.platform_saas_bot_service_activations a
    ON a.invoice_id=i.id
  WHERE i.bot_instance_id=p_bot
    AND i.kind='prepayment' AND i.status='issued'
    AND public.platform_saas_v2_remaining(i.id)=0;
  IF last_covered IS NOT NULL THEN
    IF last_covered<=p_at THEN RETURN 'held'; END IF;
  ELSIF p_at>=public.platform_saas_billable_after(owner_id) THEN
    -- Trial is over, but billing worker has not issued or confirmed
    -- this bot's initial service invoice yet.
    RETURN 'held';
  END IF;
  RETURN 'clear';
END; $f$""")

    # Only a manager of this active project may read its current commerce
    # state. The underlying bot_hold_state() stays PRIVATE and ungrantable
    # to tenant runtime to avoid leaking debt/hold data across tenants.
    op.execute(r"""
CREATE FUNCTION public.platform_saas_project_commerce_state(
    p_project uuid,p_bot uuid
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $fn$
DECLARE
    actor uuid;
    scoped uuid;
    result text;
BEGIN
    actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
    scoped:=NULLIF(current_setting('app.current_project_id',true),'')::uuid;
    IF actor IS NULL OR scoped IS DISTINCT FROM p_project
       OR p_project IS NULL OR p_bot IS NULL THEN
        RAISE EXCEPTION 'Not authorized to read project billing state'
          USING ERRCODE='42501';
    END IF;
    IF NOT EXISTS(
        SELECT 1 FROM public.projects p
        WHERE p.id=p_project AND p.status='active'
          AND (
            p.owner_user_id=actor
            OR EXISTS(
              SELECT 1 FROM public.project_members m
              WHERE m.project_id=p_project
                AND m.user_id=actor AND m.role='admin'
            )
          )
    ) THEN
        RAISE EXCEPTION 'Not authorized to read project billing state'
          USING ERRCODE='42501';
    END IF;
    SELECT public.platform_saas_bot_hold_state(b.id,clock_timestamp())
    INTO result
    FROM public.bot_instances b
    WHERE b.id=p_bot AND b.project_id=p_project AND b.status='registered';
    RETURN result;
END; $fn$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
          public.platform_saas_project_commerce_state(uuid,uuid) FROM PUBLIC
    """)
    op.execute("""
        DO $grant$ BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_project_commerce_state(uuid,uuid)
              TO membership_runtime;
          END IF;
        END $grant$
    """)



def downgrade() -> None:
    raise RuntimeError(
        "Forward-only ViMi Pay commercial hold correction; do not downgrade"
    )
