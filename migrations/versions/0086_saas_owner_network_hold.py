"""Owner-network overdue commerce holds and first-proof 12h protection.

Revision ID: 0086_saas_owner_hold
Revises: 0085_saas_proof_push

A new accepted bank proof protects an overdue account for at most 12h,
measured from its earliest proof on that invoice. Later attempts and
rejected/approved evidence cannot reset that clock. Keep existing
settlement, refunds and already bought buyer rights immutable.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0086_saas_owner_hold"
down_revision: str | None = "0085_saas_proof_push"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE FUNCTION public.platform_saas_owner_overdue_state(
  p_owner uuid,p_at timestamptz
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  enabled boolean;
  started timestamptz;
  billing_enabled boolean;
  billing_started timestamptz;
  overdue_count integer;
  unprotected_count integer;
BEGIN
  IF p_owner IS NULL OR p_at IS NULL THEN
    RAISE EXCEPTION 'Invalid owner debt check' USING ERRCODE='22023';
  END IF;
  SELECT h.enabled,h.effective_from INTO enabled,started
  FROM public.platform_saas_hold_control h WHERE h.id=1;
  SELECT c.enabled,c.effective_from INTO billing_enabled,billing_started
  FROM public.platform_saas_billing_control c WHERE c.id=1;
  IF NOT coalesce(enabled,false)
     OR NOT coalesce(billing_enabled,false)
     OR started IS NULL OR billing_started IS NULL
     OR p_at<greatest(started,billing_started)
  THEN RETURN 'disabled'; END IF;

  IF EXISTS(
    SELECT 1 FROM public.telegram_users u
    JOIN public.platform_operators po
      ON po.telegram_user_id=u.telegram_user_id
    WHERE u.id=p_owner AND po.role='owner'
  ) THEN RETURN 'clear'; END IF;

  WITH unpaid AS (
    SELECT i.id
    FROM public.platform_saas_invoices i
    WHERE i.owner_user_id=p_owner
      AND i.kind IN ('prepayment','commission')
      AND i.status='issued'
      AND i.issued_at IS NOT NULL
      AND i.issued_at>=billing_started
      AND i.due_at<=p_at
      AND public.platform_saas_v2_remaining(i.id)>0
  ), audited AS (
    SELECT i.id,
      -- All attempts for this SAME invoice share the first immutable
      -- submitted_at. A second proof cannot start a fresh 12-hour window.
      (
        SELECT min(p.submitted_at)
        FROM public.platform_saas_payment_proofs_v2 p
        JOIN public.platform_saas_payment_attempts_v2 a
          ON a.id=p.attempt_id
        WHERE a.invoice_id=i.id
      ) AS first_proof,
      EXISTS(
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
      ) AS pending_proof,
      EXISTS(
        SELECT 1 FROM public.platform_saas_payment_attempts_v2 a
        LEFT JOIN public.platform_saas_payment_rejections_v2 bad
          ON bad.attempt_id=a.id
        LEFT JOIN public.platform_saas_payment_allocations_v2 ok
          ON ok.attempt_id=a.id
        WHERE a.invoice_id=i.id
          AND (bad.attempt_id IS NOT NULL OR ok.attempt_id IS NOT NULL)
      ) AS decided_proof
    FROM unpaid i
  )
  SELECT count(*),
         count(*) FILTER(
           WHERE first_proof IS NULL
              OR first_proof + interval '12 hours'<=p_at
              OR NOT pending_proof OR decided_proof
         )
    INTO overdue_count,unprotected_count FROM audited;
  IF unprotected_count>0 THEN RETURN 'held'; END IF;
  IF overdue_count>0 THEN RETURN 'review'; END IF;
  RETURN 'clear';
END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_bot_hold_state(
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
END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_guard_new_commerce()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  scoped uuid;
  source_bot uuid;
  bot_count bigint;
  owner_id uuid;
  merchant_id uuid;
  hold_state text;
  checked_at timestamptz:=clock_timestamp();
BEGIN
  IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
  -- NEW is a trigger RECORD: branch before field access. PostgreSQL
  -- can resolve a missing field even if the other AND term is false.
  IF TG_TABLE_NAME='resources' THEN
    IF NEW.resource_type NOT IN (
      'telegram_channel','telegram_group'
    ) THEN RETURN NEW; END IF;
  END IF;
  scoped:=NULLIF(current_setting('app.current_project_id',true),'')::uuid;
  IF scoped IS DISTINCT FROM NEW.project_id THEN
    RETURN NEW; -- existing tenant RLS fails closed for foreign projects
  END IF;
  -- Another bot's debt can NEVER block connecting an unrelated new bot.
  IF TG_TABLE_NAME='bot_instances' THEN
    SELECT p.owner_user_id INTO owner_id FROM public.projects p
    WHERE p.id=NEW.project_id;
    IF owner_id IS NOT NULL AND
       public.platform_saas_owner_overdue_state(
         owner_id,checked_at
       )='held'
    THEN
      RAISE EXCEPTION 'New bot connection is on ViMi Pay owner hold'
        USING ERRCODE='P6711';
    END IF;
    RETURN NEW;
  END IF;
  SELECT p.owner_user_id,p.merchant_id INTO owner_id,merchant_id
  FROM public.projects p WHERE p.id=NEW.project_id;
  IF owner_id IS NULL THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME IN (
    'file_sale_manual_orders','file_sale_purchases'
  ) THEN
    SELECT b.id INTO source_bot FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id
      AND b.telegram_bot_id=NEW.telegram_bot_id
      AND b.status='registered';
  ELSE
    SELECT count(*),max(b.id::text)::uuid INTO bot_count,source_bot
    FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id AND b.status='registered';
    IF bot_count<>1 THEN RETURN NEW; END IF;
  END IF;
  IF source_bot IS NULL AND TG_TABLE_NAME IN (
    'file_sale_manual_orders','file_sale_purchases'
  ) THEN
    -- Even a malformed pre-insert order cannot bypass the hold by
    -- omitting the bot id. A legitimately issued order remains bound
    -- to the original Telegram bot by its own constraints.
    SELECT count(*),max(b.id::text)::uuid INTO bot_count,source_bot
    FROM public.bot_instances b
    WHERE b.project_id=NEW.project_id AND b.status='registered';
    IF bot_count<>1 THEN source_bot:=NULL; END IF;
  END IF;
  IF source_bot IS NULL THEN RETURN NEW; END IF;
  hold_state:=public.platform_saas_bot_hold_state(source_bot,checked_at);
  IF hold_state<>'held' THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME IN (
    'payment_requests','file_sale_manual_orders','file_sale_purchases'
  ) AND public.platform_saas_exemption_state(
      NEW.project_id,source_bot,merchant_id,owner_id,checked_at
  )='exempt' THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'New ViMi Pay checkout/connection is on payment hold'
    USING ERRCODE='P6711';
END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_guard_transfer_unpaid_invoice()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $guard$
BEGIN
  IF TG_OP<>'UPDATE'
     OR NEW.owner_user_id IS NOT DISTINCT FROM OLD.owner_user_id
  THEN RETURN NEW; END IF;
  -- Other bots' genuinely unpaid account debt cannot be evaded by
  -- transferring this bot to a second Telegram account.
  IF EXISTS(
    SELECT 1 FROM public.platform_saas_invoices i
    WHERE i.owner_user_id=OLD.owner_user_id
      AND i.status='issued'
      AND i.due_at<=clock_timestamp()
      AND public.platform_saas_v2_remaining(i.id)>0
  ) THEN
    RAISE EXCEPTION 'Owner debt must be reconciled before bot transfer'
      USING ERRCODE='P6712';
  END IF;
  IF EXISTS(
    SELECT 1 FROM public.bot_instances b
    WHERE b.project_id=OLD.id AND b.status='registered'
      AND (
        public.platform_saas_bot_hold_state(b.id,clock_timestamp())='held'
        OR EXISTS(
          SELECT 1 FROM public.platform_saas_invoices i
          WHERE i.bot_instance_id=b.id
            AND i.owner_user_id=OLD.owner_user_id
            AND i.kind='prepayment' AND i.status='issued'
            AND public.platform_saas_v2_remaining(i.id)>0
        )
      )
  ) THEN
    RAISE EXCEPTION 'Unpaid ViMi Pay service must be reconciled before transfer'
      USING ERRCODE='P6712';
  END IF;
  RETURN NEW;
END; $guard$
    """)
    # The legacy central debt report must reflect the NEW account-wide
    # authoritative same-state function; no stale seven-day grace metric.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_owner_hold_state(
  p_owner uuid,p_at timestamptz
) RETURNS text LANGUAGE sql SECURITY DEFINER
SET search_path=pg_catalog,public AS $state$
  SELECT CASE public.platform_saas_owner_overdue_state(p_owner,p_at)
    WHEN 'review' THEN 'grace'
    ELSE public.platform_saas_owner_overdue_state(p_owner,p_at)
  END;
$state$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
      public.platform_saas_owner_overdue_state(uuid,timestamptz) FROM PUBLIC
    """)
    op.execute("""
      DO $grants$
      BEGIN
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
        THEN
          -- Internal guards use SECURITY DEFINER; the runtime cannot
          -- bypass owner-wide holds by directly invoking the helper.
          REVOKE ALL ON FUNCTION
            public.platform_saas_owner_overdue_state(uuid,timestamptz)
            FROM membership_runtime;
        END IF;
      END $grants$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Commercial owner-network holds cannot be blindly rolled back"
    )
