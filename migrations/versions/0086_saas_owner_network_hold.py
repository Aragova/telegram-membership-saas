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
  -- Due debt of this owner blocks new bots, even under another project.
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
      AND EXISTS(
        SELECT 1 FROM public.platform_saas_billing_control c
        WHERE c.id=1 AND c.enabled
          AND c.effective_from IS NOT NULL
          AND i.issued_at>=c.effective_from
      )
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
    # Expose actual due time, first-proof deadline and upcoming bill dates
    # through the existing tenant-scoped Rich bill, without a second menu.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_merchant_bill_summary(
  p_month date,p_project uuid DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
  actor uuid;
  bot_id uuid;
  bots integer;
  bill record;
  turnover jsonb;
  service_rub numeric:=0;
  subscriptions_rub numeric:=0;
  files_rub numeric:=0;
  remaining numeric:=0;
  received numeric:=0;
  service_native numeric:=0;
  service_currency text:='RUB';
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  IF actor IS NULL OR p_project IS NULL THEN
    RAISE EXCEPTION 'Select one owned ViMi Pay bot'
      USING ERRCODE='42501';
  END IF;
  IF NOT EXISTS(
    SELECT 1 FROM public.projects p
    WHERE p.id=p_project AND p.owner_user_id=actor
  ) THEN RAISE EXCEPTION 'Bot ownership required' USING ERRCODE='42501'; END IF;
  SELECT count(*),max(b.id::text)::uuid INTO bots,bot_id
  FROM public.bot_instances b
  WHERE b.project_id=p_project AND b.status='registered';
  IF bots<>1 THEN
    RAISE EXCEPTION 'One active bot per project required'
      USING ERRCODE='42501';
  END IF;
  SELECT i.* INTO bill
  FROM public.platform_saas_invoices i
  WHERE i.bot_instance_id=bot_id AND i.owner_user_id=actor
    AND i.status='issued' AND i.kind='prepayment'
  ORDER BY i.period_start DESC LIMIT 1;
  IF bill.id IS NULL THEN
    RETURN jsonb_build_object('bot_id',bot_id,'project_id',p_project,
      'invoice_id',NULL,'period_start',NULL,'period_end',NULL,
      'due_at',NULL,
      'upcoming_at',CASE
        WHEN (SELECT c.enabled FROM public.platform_saas_billing_control c
              WHERE c.id=1) THEN public.platform_saas_billable_after(actor)
        ELSE NULL END,
      'hold_state',public.platform_saas_owner_overdue_state(
        actor,clock_timestamp()
      ),
      'turnover','[]'::jsonb,'service_rub','0',
      'service_native','0','service_currency','RUB',
      'subscription_rub','0','file_rub','0','total_rub','0',
      'paid_rub','0','credit_rub','0',
      'total_due_rub','0','first_period',true);
  END IF;
  SELECT
    coalesce(sum(l.charge_rub) FILTER(
      WHERE l.source_kind='bot_fee'),0),
    coalesce(sum(l.charge_rub) FILTER(
      WHERE l.source_kind IN ('subscription','subscription_stars',
                              'subscription_crypto')),0),
    coalesce(sum(l.charge_rub) FILTER(
      WHERE l.source_kind IN ('file_manual','file_stars','file_crypto')),0)
  INTO service_rub,subscriptions_rub,files_rub
  FROM public.platform_saas_invoice_lines l WHERE l.invoice_id=bill.id;
  SELECT l.original_amount,l.original_currency
  INTO service_native,service_currency
  FROM public.platform_saas_invoice_lines l
  WHERE l.invoice_id=bill.id AND l.source_kind='bot_fee'
  LIMIT 1;
  WITH groups AS (
    SELECT CASE WHEN l.source_kind LIKE 'subscription%' THEN 'subscription'
                ELSE 'file' END AS kind,
           l.original_currency AS currency,
           count(*)::integer AS sold,sum(l.original_amount) AS gross
    FROM public.platform_saas_invoice_lines l
    WHERE l.invoice_id=bill.id AND l.source_kind<>'bot_fee'
    GROUP BY 1,2
  )
  SELECT coalesce(jsonb_agg(jsonb_build_object(
    'kind',kind,'currency',currency,'sold',sold,'gross',gross::text
  ) ORDER BY kind,currency),'[]'::jsonb) INTO turnover FROM groups;
  remaining:=public.platform_saas_v2_remaining(bill.id);
  SELECT coalesce(sum(r.amount_rub),0) INTO received
    FROM public.platform_saas_receipts r WHERE r.invoice_id=bill.id;
  received:=received+coalesce((
    SELECT sum(a.allocated_rub)
    FROM public.platform_saas_payment_allocations_v2 a
    WHERE a.invoice_id=bill.id
  ),0);
  RETURN jsonb_build_object(
    'bot_id',bot_id,'project_id',p_project,'invoice_id',bill.id,
    'due_at',bill.due_at,
    'upcoming_at',CASE WHEN remaining=0 THEN coalesce((
      SELECT a.effective_end
      FROM public.platform_saas_bot_service_activations a
      WHERE a.invoice_id=bill.id
    ),bill.period_end) ELSE NULL END,
    'hold_state',public.platform_saas_owner_overdue_state(
       actor,clock_timestamp()
    ),
    'review_until',CASE WHEN remaining>0 THEN (
      SELECT min(p.submitted_at)+interval '12 hours'
      FROM public.platform_saas_payment_proofs_v2 p
      JOIN public.platform_saas_payment_attempts_v2 a
        ON a.id=p.attempt_id
      WHERE a.invoice_id=bill.id
        AND NOT EXISTS (
          SELECT 1 FROM public.platform_saas_payment_rejections_v2 bad
          JOIN public.platform_saas_payment_attempts_v2 ra
            ON ra.id=bad.attempt_id
          WHERE ra.invoice_id=bill.id
        )
        AND NOT EXISTS (
          SELECT 1 FROM public.platform_saas_payment_allocations_v2 ok
          JOIN public.platform_saas_payment_attempts_v2 oa
            ON oa.id=ok.attempt_id
          WHERE oa.invoice_id=bill.id
        )
        AND EXISTS (
          SELECT 1 FROM public.platform_saas_payment_proofs_v2 pending
          JOIN public.platform_saas_payment_attempts_v2 aa
            ON aa.id=pending.attempt_id
          WHERE aa.invoice_id=bill.id AND
            NOT EXISTS (
              SELECT 1 FROM public.platform_saas_payment_allocations_v2 yes
              WHERE yes.attempt_id=aa.id
            ) AND NOT EXISTS (
              SELECT 1 FROM public.platform_saas_payment_rejections_v2 no
              WHERE no.attempt_id=aa.id
            )
        )
    ) ELSE NULL END,
    'period_start',coalesce((
      SELECT a.effective_start
      FROM public.platform_saas_bot_service_activations a
      WHERE a.invoice_id=bill.id
    ),bill.period_start),
    'period_end',coalesce((
      SELECT a.effective_end
      FROM public.platform_saas_bot_service_activations a
      WHERE a.invoice_id=bill.id
    ),bill.period_end),
    'turnover',turnover,'service_rub',service_rub::text,
    'service_native',service_native::text,
    'service_currency',service_currency,
    'subscription_rub',round(subscriptions_rub,2)::text,
    'file_rub',round(files_rub,2)::text,'total_rub',bill.total_rub::text,
    'paid_rub',received::text,
    'credit_rub',(bill.total_rub-remaining-received)::text,
    'total_due_rub',remaining::text,
    'first_period',NOT EXISTS(
      SELECT 1 FROM public.platform_saas_invoices prior
      WHERE prior.bot_instance_id=bot_id AND prior.owner_user_id=actor
        AND prior.kind='prepayment' AND prior.status='issued'
        AND coalesce((
          SELECT a.effective_end
          FROM public.platform_saas_bot_service_activations a
          WHERE a.invoice_id=prior.id
        ),prior.period_end)=bill.period_start
    )
  );
END; $f$
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
