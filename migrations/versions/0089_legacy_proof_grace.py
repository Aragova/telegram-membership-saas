"""Allow pending original bank proof to protect one grandfathered bot.

Revision ID: 0089_legacy_proof_grace
Revises: 0088_backup_monitor

A pre-activation issued invoice can be held for that bot but is intentionally
ignored by owner-wide commercial debt. Grant the existing first-proof 12-hour
review respite ONLY for that one bot. Never retroactively charge/hold the
owner's other bots and never edit existing issued financial records.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0089_legacy_proof_grace"
down_revision: str | None = "0088_backup_monitor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Invisible, durable mapping: the original Telegram bot message ID is
    # the payment prompt's identifier. No technical reference in user text.
    op.create_table(
        "platform_saas_payment_prompts",
        sa.Column("source_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_payment_attempts_v2.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.PrimaryKeyConstraint(
            "source_bot_id", "source_chat_id", "source_message_id",
            name="pk_saas_payment_prompt_source",
        ),
        sa.UniqueConstraint(
            "attempt_id", name="uq_saas_payment_prompt_attempt",
        ),
    )
    op.execute("REVOKE ALL ON public.platform_saas_payment_prompts FROM PUBLIC")
    op.execute("""
        ALTER TABLE public.platform_saas_payment_prompts
        ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.platform_saas_payment_prompts
        FORCE ROW LEVEL SECURITY
    """)
    # The SECURITY DEFINER functions run as the trusted table owner.
    # Even if runtime DML is accidentally granted in the future, the
    # caller role cannot see or forge another tenant's private binding.
    op.execute("""
        CREATE POLICY prompt_trusted_table_owner
        ON public.platform_saas_payment_prompts
        FOR ALL TO PUBLIC
        USING (
            current_user = (
                SELECT pg_get_userbyid(c.relowner)
                FROM pg_catalog.pg_class c
                WHERE c.oid = 'public.platform_saas_payment_prompts'::regclass
            )
        )
        WITH CHECK (
            current_user = (
                SELECT pg_get_userbyid(c.relowner)
                FROM pg_catalog.pg_class c
                WHERE c.oid = 'public.platform_saas_payment_prompts'::regclass
            )
        )
    """)
    op.execute("""
        DO $revoke$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            REVOKE ALL ON public.platform_saas_payment_prompts
              FROM membership_runtime;
          END IF;
        END $revoke$
    """)
    op.execute(r"""
CREATE FUNCTION public.platform_saas_bind_payment_prompt(
    p_attempt uuid,p_bot bigint,p_chat bigint,p_message bigint
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; account bigint;
BEGIN
    actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
    SELECT u.telegram_user_id INTO account
    FROM public.telegram_users u WHERE u.id=actor;
    IF actor IS NULL OR account IS DISTINCT FROM p_chat
       OR p_bot IS NULL OR p_bot<=0 OR p_message IS NULL OR p_message<=0
       OR NOT EXISTS(
         SELECT 1 FROM public.platform_saas_payment_attempts_v2 a
         JOIN public.platform_saas_invoices i ON i.id=a.invoice_id
         WHERE a.id=p_attempt AND a.owner_user_id=actor
           AND i.owner_user_id=actor AND i.status='issued'
           AND a.expires_at>clock_timestamp()
       ) THEN
       RAISE EXCEPTION 'Payment prompt owner/source invalid'
         USING ERRCODE='42501';
    END IF;
    INSERT INTO public.platform_saas_payment_prompts(
      source_bot_id,source_chat_id,source_message_id,attempt_id
    ) VALUES(p_bot,p_chat,p_message,p_attempt);
    RETURN true;
END; $f$
    """)
    op.execute(r"""
CREATE FUNCTION public.platform_saas_resolve_payment_prompt(
    p_bot bigint,p_chat bigint,p_message bigint
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE actor uuid; account bigint; result uuid;
BEGIN
    actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
    SELECT u.telegram_user_id INTO account
    FROM public.telegram_users u WHERE u.id=actor;
    IF actor IS NULL OR p_chat IS DISTINCT FROM account
       OR p_bot IS NULL OR p_bot<=0 OR p_message IS NULL OR p_message<=0 THEN
      RAISE EXCEPTION 'Payment prompt owner/source invalid'
        USING ERRCODE='42501';
    END IF;
    SELECT a.id INTO result
    FROM public.platform_saas_payment_prompts m
    JOIN public.platform_saas_payment_attempts_v2 a
      ON a.id=m.attempt_id
    JOIN public.platform_saas_invoices i ON i.id=a.invoice_id
    WHERE m.source_bot_id=p_bot AND m.source_chat_id=p_chat
      AND m.source_message_id=p_message
      AND a.owner_user_id=actor AND i.owner_user_id=actor
      AND i.status='issued'
      AND NOT EXISTS(
        SELECT 1 FROM public.platform_saas_payment_rejections_v2 r
        WHERE r.attempt_id=a.id
      );
    RETURN result;
END; $f$
    """)
    for function in (
        "platform_saas_bind_payment_prompt(uuid,bigint,bigint,bigint)",
        "platform_saas_resolve_payment_prompt(bigint,bigint,bigint)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
        op.execute(f"""
            DO $grant$ BEGIN
              IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
              THEN GRANT EXECUTE ON FUNCTION public.{function}
                   TO membership_runtime;
              END IF;
            END $grant$
        """)

    # Existing legacy bank-reference approvals continue unchanged. For the
    # explicit one-click OPERATOR bank verification, distinguish the Telegram
    # proof identity from an actual bank transaction reference in the ledger.
    op.alter_column(
        "platform_saas_payment_allocations_v2", "bank_reference",
        existing_type=sa.String(160), nullable=True,
    )
    op.drop_constraint(
        "ck_saas_v2_bank_reference",
        "platform_saas_payment_allocations_v2", type_="check",
    )
    op.create_check_constraint(
        "ck_saas_v2_bank_reference",
        "platform_saas_payment_allocations_v2",
        "bank_reference IS NULL OR "
        "length(btrim(bank_reference)) BETWEEN 8 AND 160",
    )
    op.add_column(
        "platform_saas_payment_allocations_v2",
        sa.Column("operator_evidence_key", sa.String(80), nullable=True),
    )
    op.create_unique_constraint(
        "uq_saas_v2_operator_evidence",
        "platform_saas_payment_allocations_v2",
        ["operator_evidence_key"],
    )
    op.create_check_constraint(
        "ck_saas_v2_allocation_evidence",
        "platform_saas_payment_allocations_v2",
        "bank_reference IS NOT NULL OR operator_evidence_key IS NOT NULL",
    )
    op.execute(r"""
CREATE FUNCTION public.platform_saas_v2_approve_checked_attempt(
    p_attempt uuid
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
    actor uuid;
    payment record;
    invoice record;
    evidence record;
    prior record;
    used numeric;
    key_value text;
    allocation_id uuid;
BEGIN
    actor:=public.platform_saas_require_owner();
    IF p_attempt IS NULL THEN
        RAISE EXCEPTION 'Missing original payment attempt'
          USING ERRCODE='22023';
    END IF;
    SELECT * INTO payment
    FROM public.platform_saas_payment_attempts_v2
    WHERE id=p_attempt FOR UPDATE;
    IF payment.id IS NULL THEN
        RAISE EXCEPTION 'Unknown payment attempt' USING ERRCODE='42501';
    END IF;
    SELECT * INTO invoice FROM public.platform_saas_invoices
    WHERE id=payment.invoice_id FOR UPDATE;
    IF invoice.id IS NULL OR invoice.status<>'issued'
       OR invoice.owner_user_id IS DISTINCT FROM payment.owner_user_id THEN
        RAISE EXCEPTION 'Bill changed before operator approval'
          USING ERRCODE='42501';
    END IF;
    SELECT * INTO prior
    FROM public.platform_saas_payment_allocations_v2
    WHERE attempt_id=p_attempt;
    IF prior.id IS NOT NULL THEN RETURN prior.id; END IF;
    IF EXISTS(
        SELECT 1 FROM public.platform_saas_payment_rejections_v2 r
        WHERE r.attempt_id=p_attempt
    ) THEN
        RAISE EXCEPTION 'Proof already rejected' USING ERRCODE='42501';
    END IF;
    SELECT * INTO evidence FROM public.platform_saas_payment_proofs_v2 p
    WHERE p.attempt_id=p_attempt;
    IF evidence.id IS NULL THEN
        RAISE EXCEPTION 'Proof missing' USING ERRCODE='42501';
    END IF;
    -- This is NOT a bank transaction ID. It only prevents reusing the
    -- IDENTICAL Telegram file as proof across multiple invoices.
    -- The operator must check actual funds independently.
    -- file_unique_id is stable across bots and rotated Telegram file_id
    -- values. Old proof rows without it fall back to a bot-scoped file_id.
    key_value:='tg-proof:'||md5(
        CASE WHEN evidence.source_file_unique_id IS NOT NULL
          THEN evidence.source_file_unique_id
          ELSE evidence.source_bot_id::text||':'||evidence.source_file_id
        END
    );
    SELECT coalesce(sum(r.amount_rub),0) INTO used
    FROM public.platform_saas_receipts r
    WHERE r.invoice_id=invoice.id;
    SELECT used+coalesce(sum(a.allocated_rub),0) INTO used
    FROM public.platform_saas_payment_allocations_v2 a
    WHERE a.invoice_id=invoice.id;
    IF used+payment.amount_rub>invoice.total_rub THEN
        RAISE EXCEPTION 'Payment exceeds remaining invoice amount'
          USING ERRCODE='23514';
    END IF;
    allocation_id:=gen_random_uuid();
    INSERT INTO public.platform_saas_payment_allocations_v2(
        id,attempt_id,invoice_id,received_currency,received_amount,
        allocated_rub,bank_reference,operator_evidence_key,confirmed_by
    ) VALUES(
        allocation_id,p_attempt,invoice.id,
        payment.currency_code,payment.amount_currency,payment.amount_rub,
        NULL,key_value,actor
    );
    RETURN allocation_id;
END; $f$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
          public.platform_saas_v2_approve_checked_attempt(uuid) FROM PUBLIC
    """)
    op.execute("""
        DO $grant$ BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_v2_approve_checked_attempt(uuid)
              TO membership_runtime;
          END IF;
        END $grant$
    """)

    # One canonical Telegram media identity for NEW proofs, stable across
    # bot tokens and valid file_id rotations. Existing historical proofs keep
    # NULL and can still be reviewed with the previous evidence fallback.
    op.add_column(
        "platform_saas_payment_proofs_v2",
        sa.Column("source_file_unique_id", sa.String(128), nullable=True),
    )
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



    # Preserve the old 6-argument proof RPC for already-delivered messages.
    # The new 7-argument function writes file_unique_id in its ORIGINAL
    # INSERT. Proof rows are immutable and must never be UPDATEd.
    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_submit_proof(
      p_attempt uuid,p_bot bigint,p_chat bigint,p_message bigint,
      p_file_id text,p_kind text,p_unique_id text
    ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; original record; existing record; proof uuid;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      -- Serialize against verified-refund allocation. Without the shared
      -- advisory lock, a proof racing quote expiry could lose its credit.
      PERFORM pg_advisory_xact_lock(
        hashtextextended('vimi:saas:verified-refunds',0)
      );
      IF p_unique_id IS NULL
         OR p_unique_id !~ '^[A-Za-z0-9_-]{8,128}$' THEN
        RAISE EXCEPTION 'Canonical Telegram file identity required'
          USING ERRCODE='22023';
      END IF;
      SELECT * INTO original
      FROM public.platform_saas_payment_attempts_v2
      WHERE id=p_attempt FOR UPDATE;
      IF original.id IS NULL OR original.owner_user_id IS DISTINCT FROM actor
         OR p_bot IS NULL OR p_bot<=0 OR p_chat IS NULL OR p_chat<=0
         OR p_chat IS DISTINCT FROM (
           SELECT u.telegram_user_id FROM public.telegram_users u
           WHERE u.id=actor
         )
         OR p_message IS NULL OR p_message<=0
         OR length(coalesce(p_file_id,'')) NOT BETWEEN 8 AND 512
         OR p_kind NOT IN ('photo','document')
         OR clock_timestamp()>original.expires_at THEN
        RAISE EXCEPTION 'Original payer proof is unavailable'
          USING ERRCODE='42501';
      END IF;
      SELECT * INTO existing
      FROM public.platform_saas_payment_proofs_v2
      WHERE attempt_id=p_attempt;
      IF existing.id IS NOT NULL THEN
        IF existing.source_bot_id IS DISTINCT FROM p_bot
           OR existing.source_chat_id IS DISTINCT FROM p_chat
           OR existing.source_message_id IS DISTINCT FROM p_message
           OR existing.source_file_id IS DISTINCT FROM p_file_id
           OR existing.file_type IS DISTINCT FROM p_kind
           OR existing.source_file_unique_id IS DISTINCT FROM p_unique_id THEN
          RAISE EXCEPTION 'Original proof identity conflict'
            USING ERRCODE='23505';
        END IF;
        RETURN existing.id;
      END IF;
      proof:=gen_random_uuid();
      INSERT INTO public.platform_saas_payment_proofs_v2(
        id,attempt_id,source_bot_id,source_chat_id,
        source_message_id,source_file_id,source_file_unique_id,file_type
      ) VALUES(
        proof,p_attempt,p_bot,p_chat,p_message,p_file_id,p_unique_id,p_kind
      );
      RETURN proof;
    END; $f$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
          public.platform_saas_v2_submit_proof(
            uuid,bigint,bigint,bigint,text,text,text
          ) FROM PUBLIC
    """)
    op.execute("""
        DO $grant$ BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_v2_submit_proof(
                uuid,bigint,bigint,bigint,text,text,text
              ) TO membership_runtime;
          END IF;
        END $grant$
    """)

    # Owner-only source reader now returns the stable Telegram evidence ID,
    # never a decrypted bot token or media contents.
    op.execute("""
    CREATE OR REPLACE FUNCTION public.platform_saas_pending_proof_source(
      p_attempt uuid
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE payload jsonb;
    BEGIN
      PERFORM public.platform_saas_require_owner();
      IF p_attempt IS NULL THEN
        RAISE EXCEPTION 'Missing payment attempt' USING ERRCODE='22023';
      END IF;
      SELECT jsonb_build_object(
        'id',a.id,'invoice_id',a.invoice_id,
        'amount',a.amount_currency::text,'currency',a.currency_code,
        'instructions',a.instructions_snapshot,
        'source_bot',p.source_bot_id,
        'source_project',b.project_id,
        'source_file',p.source_file_id,
        'source_file_unique_id',p.source_file_unique_id,
        'file_type',p.file_type
      ) INTO payload
      FROM public.platform_saas_payment_attempts_v2 a
      JOIN public.platform_saas_payment_proofs_v2 p
        ON p.attempt_id=a.id
      LEFT JOIN public.bot_instances b
        ON b.telegram_bot_id=p.source_bot_id AND b.status='registered'
      WHERE a.id=p_attempt
        AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_payment_allocations_v2 ok
          WHERE ok.attempt_id=a.id
        )
        AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_payment_rejections_v2 no
          WHERE no.attempt_id=a.id
        );
      RETURN payload;
    END; $f$
    """)

def downgrade() -> None:
    raise RuntimeError(
        "Forward-only ViMi Pay commercial hold correction; do not downgrade"
    )
