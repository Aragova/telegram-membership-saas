"""Append-only partial bank payment attempts for the same ViMi Pay billing UI.

Revision ID: 0067_saas_partial_payments
Revises: 0066_commercial_v2
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_saas_partial_payments"
down_revision: str | None = "0066_commercial_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A single central method per currency; NEVER one bank catalog per bot.
    op.create_table(
        "platform_saas_payment_methods_v2",
        sa.Column("currency_code", sa.String(3), primary_key=True),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint(
            "currency_code IN ('RUB','USD','EUR','UAH','BYN')",
            name="ck_saas_v2_method_currency",
        ),
        sa.CheckConstraint(
            "length(btrim(instructions)) BETWEEN 8 AND 900",
            name="ck_saas_v2_method_instructions",
        ),
    )
    op.create_table(
        "platform_saas_payment_attempts_v2",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("invoice_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_invoices.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("amount_rub", sa.Numeric(38, 2), nullable=False),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("rub_per_unit", sa.Numeric(38, 18), nullable=False),
        sa.Column("amount_currency", sa.Numeric(38, 2), nullable=False),
        sa.Column("quote_date", sa.Date(), nullable=False),
        sa.Column("source_date", sa.Date()),
        sa.Column("instructions_snapshot", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "amount_rub>0 AND amount_currency>0 AND rub_per_unit>0",
            name="ck_saas_v2_attempt_positive",
        ),
        sa.CheckConstraint(
            "currency_code IN ('RUB','USD','EUR','UAH','BYN')",
            name="ck_saas_v2_attempt_currency",
        ),
    )
    op.create_index("ix_saas_v2_attempt_invoice",
                    "platform_saas_payment_attempts_v2",
                    ["invoice_id", "created_at"])
    op.create_table(
        "platform_saas_payment_proofs_v2",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_payment_attempts_v2.id", ondelete="RESTRICT",
        ), unique=True, nullable=False),
        sa.Column("source_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("source_file_id", sa.String(512), nullable=False),
        sa.Column("file_type", sa.String(12), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("source_bot_id", "source_message_id",
                            name="uq_saas_v2_proof_original_message"),
        sa.CheckConstraint(
            "source_bot_id>0 AND source_message_id>0 "
            "AND file_type IN ('photo','document')",
            name="ck_saas_v2_proof_media",
        ),
    )
    op.create_table(
        "platform_saas_payment_allocations_v2",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_payment_attempts_v2.id", ondelete="RESTRICT",
        ), unique=True, nullable=False),
        sa.Column("invoice_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_invoices.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("received_currency", sa.String(3), nullable=False),
        sa.Column("received_amount", sa.Numeric(38, 2), nullable=False),
        sa.Column("allocated_rub", sa.Numeric(38, 2), nullable=False),
        sa.Column("bank_reference", sa.String(160), nullable=False, unique=True),
        sa.Column("confirmed_by", sa.Uuid(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint(
            "allocated_rub>0 AND received_amount>0",
            name="ck_saas_v2_allocation_positive",
        ),
        sa.CheckConstraint(
            "length(btrim(bank_reference)) BETWEEN 8 AND 160",
            name="ck_saas_v2_bank_reference",
        ),
    )
    op.create_index("ix_saas_v2_alloc_invoice",
                    "platform_saas_payment_allocations_v2",
                    ["invoice_id", "confirmed_at"])
    op.create_table(
        "platform_saas_payment_rejections_v2",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_payment_attempts_v2.id", ondelete="RESTRICT",
        ), unique=True, nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("rejected_by", sa.Uuid(), nullable=False),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 5 AND 500",
            name="ck_saas_v2_rejection_reason",
        ),
    )
    for table in (
        "platform_saas_payment_methods_v2",
        "platform_saas_payment_attempts_v2",
        "platform_saas_payment_proofs_v2",
        "platform_saas_payment_allocations_v2",
        "platform_saas_payment_rejections_v2",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute("""
      DO $revoke$
      BEGIN
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
          REVOKE ALL ON public.platform_saas_payment_methods_v2,
            public.platform_saas_payment_attempts_v2,
            public.platform_saas_payment_proofs_v2,
            public.platform_saas_payment_allocations_v2,
            public.platform_saas_payment_rejections_v2
            FROM membership_runtime;
        END IF;
      END $revoke$
    """)
    # Permanent audit: attempts, original proof, confirmed money and rejections
    # cannot be edited after Telegram callbacks, service retries or transfers.
    for table in (
        "platform_saas_payment_attempts_v2",
        "platform_saas_payment_proofs_v2",
        "platform_saas_payment_allocations_v2",
        "platform_saas_payment_rejections_v2",
    ):
        op.execute(f"""
          CREATE TRIGGER trg_{table}_immutable
          BEFORE UPDATE OR DELETE ON public.{table}
          FOR EACH ROW EXECUTE FUNCTION
            public.platform_saas_credit_entry_immutable()
        """)

    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_bank_method(
       p_currency text,p_instructions text
    ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid;
    BEGIN
      actor:=public.platform_saas_require_owner();
      IF p_currency NOT IN ('RUB','USD','EUR','UAH','BYN')
         OR (p_instructions IS NOT NULL AND
             length(btrim(p_instructions)) NOT BETWEEN 8 AND 900) THEN
        RAISE EXCEPTION 'Invalid central bank instructions'
          USING ERRCODE='22023';
      END IF;
      IF p_instructions IS NULL THEN
        UPDATE public.platform_saas_payment_methods_v2
        SET enabled=false,updated_at=clock_timestamp(),updated_by=actor
        WHERE currency_code=p_currency;
      ELSE
        INSERT INTO public.platform_saas_payment_methods_v2(
          currency_code,instructions,enabled,updated_by
        ) VALUES(p_currency,btrim(p_instructions),true,actor)
        ON CONFLICT(currency_code) DO UPDATE SET
          instructions=excluded.instructions,enabled=true,
          updated_by=actor,updated_at=clock_timestamp();
      END IF;
      RETURN true;
    END; $f$
    """)
    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_invoice_balance(
       p_invoice uuid
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; operator_access boolean; i record;
            legacy_paid numeric; allocated numeric; remaining numeric;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      SELECT EXISTS(SELECT 1 FROM public.telegram_users u
        JOIN public.platform_operators o ON o.telegram_user_id=u.telegram_user_id
        WHERE u.id=actor AND o.role='owner') INTO operator_access;
      SELECT * INTO i FROM public.platform_saas_invoices
      WHERE id=p_invoice;
      IF i.id IS NULL OR i.status<>'issued'
         OR (i.owner_user_id IS DISTINCT FROM actor AND NOT operator_access) THEN
        RAISE EXCEPTION 'Invoice unavailable' USING ERRCODE='42501';
      END IF;
      SELECT coalesce(sum(amount_rub),0) INTO legacy_paid
        FROM public.platform_saas_receipts WHERE invoice_id=i.id;
      SELECT coalesce(sum(allocated_rub),0) INTO allocated
        FROM public.platform_saas_payment_allocations_v2
        WHERE invoice_id=i.id;
      remaining:=i.total_rub-legacy_paid-allocated;
      IF remaining<0 THEN
        RAISE EXCEPTION 'Overpaid bill: accounting inconsistent'
          USING ERRCODE='23514';
      END IF;
      RETURN jsonb_build_object(
        'invoice_id',i.id,'owner_user_id',i.owner_user_id,
        'total_rub',i.total_rub::text,
        'settled_rub',(legacy_paid+allocated)::text,
        'remaining_rub',remaining::text,
        'paid',remaining=0,'partial_rub',allocated::text,
        'legacy_rub',legacy_paid::text
      );
    END; $f$
    """)


    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_create_attempt(
      p_invoice uuid,p_requested_rub numeric,p_currency text
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE
      actor uuid;
      item record;
      paid numeric;
      due numeric;
      rate numeric;
      fx_day date;
      day date;
      bank text;
      converted numeric;
      snapped_rub numeric;
      attempt uuid;
      deadline timestamptz;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      IF actor IS NULL OR p_invoice IS NULL
         OR p_currency NOT IN ('RUB','USD','EUR','UAH','BYN')
         OR p_requested_rub IS NULL OR p_requested_rub<=0
         OR p_requested_rub<>round(p_requested_rub,2) THEN
        RAISE EXCEPTION 'Invalid payment amount/currency'
          USING ERRCODE='22023';
      END IF;
      SELECT * INTO item FROM public.platform_saas_invoices
        WHERE id=p_invoice FOR UPDATE;
      IF item.id IS NULL OR item.status<>'issued'
         OR item.owner_user_id IS DISTINCT FROM actor THEN
        RAISE EXCEPTION 'Account invoice is not payable'
          USING ERRCODE='42501';
      END IF;
      SELECT coalesce(sum(amount_rub),0) INTO paid
      FROM public.platform_saas_receipts WHERE invoice_id=p_invoice;
      SELECT paid+coalesce(sum(allocated_rub),0) INTO paid
      FROM public.platform_saas_payment_allocations_v2
      WHERE invoice_id=p_invoice;
      due:=item.total_rub-paid;
      IF p_requested_rub>due THEN
        RAISE EXCEPTION 'Requested amount exceeds unpaid invoice'
          USING ERRCODE='23514';
      END IF;
      SELECT instructions INTO bank
      FROM public.platform_saas_payment_methods_v2
      WHERE currency_code=p_currency AND enabled;
      IF bank IS NULL AND p_currency='RUB' THEN
        SELECT instructions INTO bank
        FROM public.platform_saas_payment_details WHERE id=1;
      END IF;
      IF bank IS NULL THEN
        RAISE EXCEPTION 'Platform bank details not published'
          USING ERRCODE='42501';
      END IF;
      day:=(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date;
      IF p_currency='RUB' THEN rate:=1;fx_day:=NULL;
      ELSE
        SELECT rub_per_unit,source_date INTO rate,fx_day
        FROM public.platform_saas_daily_fx_rates
        WHERE rate_date=day AND currency_code=p_currency
          AND source_name='CBR' AND source_date<=day
          AND source_date>=day-interval '14 days';
      END IF;
      IF rate IS NULL OR rate<=0 THEN
        RAISE EXCEPTION 'Verified bank currency conversion unavailable'
          USING ERRCODE='42501';
      END IF;
      -- Foreign currency is paid in 2 decimal places. A rounded bank
      -- transfer can differ slightly from the RUB obligation. Preserve the
      -- exact RUB amount requested; the platform absorbs that bounded FX
      -- rounding difference rather than creating an unpayable residual.
      converted:=round(p_requested_rub/rate,2);
      snapped_rub:=p_requested_rub;
      IF converted<=0 OR snapped_rub>due
         OR abs(converted*rate-snapped_rub)>rate*0.005+0.005 THEN
        RAISE EXCEPTION 'Converted payment amount outside remaining debt'
          USING ERRCODE='23514';
      END IF;
      deadline:=clock_timestamp()+interval '48 hours';
      attempt:=gen_random_uuid();
      INSERT INTO public.platform_saas_payment_attempts_v2(
        id,invoice_id,owner_user_id,amount_rub,currency_code,
        rub_per_unit,amount_currency,quote_date,source_date,
        instructions_snapshot,expires_at
      ) VALUES(
        attempt,item.id,actor,snapped_rub,p_currency,rate,
        converted,day,fx_day,bank,deadline
      );
      RETURN jsonb_build_object(
        'id',attempt,'invoice_id',item.id,'amount_rub',snapped_rub::text,
        'currency',p_currency,'amount',converted::text,
        'rub_per_unit',rate::text,'quote_date',day,
        'source_date',fx_day,'expires_at',deadline,
        'instructions',bank,'remaining_before',due::text
      );
    END; $f$
    """)
    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_submit_proof(
      p_attempt uuid,p_bot bigint,p_message bigint,
      p_file_id text,p_kind text
    ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; original record; existing uuid; proof uuid;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      SELECT * INTO original
      FROM public.platform_saas_payment_attempts_v2 WHERE id=p_attempt;
      IF original.id IS NULL OR original.owner_user_id IS DISTINCT FROM actor
         OR p_bot IS NULL OR p_bot<=0 OR p_message IS NULL OR p_message<=0
         OR length(coalesce(p_file_id,'')) NOT BETWEEN 8 AND 512
         OR p_kind NOT IN ('photo','document')
         OR clock_timestamp()>original.expires_at THEN
        RAISE EXCEPTION 'Original payer proof is unavailable'
          USING ERRCODE='42501';
      END IF;
      SELECT id INTO existing FROM public.platform_saas_payment_proofs_v2
      WHERE attempt_id=p_attempt;
      IF existing IS NOT NULL THEN RETURN existing; END IF;
      proof:=gen_random_uuid();
      INSERT INTO public.platform_saas_payment_proofs_v2(
        id,attempt_id,source_bot_id,source_message_id,
        source_file_id,file_type
      ) VALUES(
        proof,p_attempt,p_bot,p_message,p_file_id,p_kind
      );
      RETURN proof;
    END; $f$
    """)


    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_approve_attempt(
      p_attempt uuid,p_reference text
    ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; original record; invoice record;
            existing record; used numeric; allocated_id uuid;
    BEGIN
      actor:=public.platform_saas_require_owner();
      IF p_attempt IS NULL
         OR length(btrim(coalesce(p_reference,''))) NOT BETWEEN 8 AND 160 THEN
        RAISE EXCEPTION 'Verified bank receipt reference required'
          USING ERRCODE='22023';
      END IF;
      -- Approval and rejection must lock the SAME row before reading
      -- opposite outcomes. Invoice lock remains second, for balance safety.
      SELECT * INTO original FROM public.platform_saas_payment_attempts_v2
      WHERE id=p_attempt FOR UPDATE;
      IF original.id IS NULL THEN
        RAISE EXCEPTION 'Unknown partial payment attempt'
          USING ERRCODE='42501';
      END IF;
      SELECT * INTO invoice FROM public.platform_saas_invoices
      WHERE id=original.invoice_id FOR UPDATE;
      IF invoice.id IS NULL OR invoice.status<>'issued'
         OR invoice.owner_user_id IS DISTINCT FROM original.owner_user_id THEN
        RAISE EXCEPTION 'Original bill/owner has changed'
          USING ERRCODE='42501';
      END IF;
      SELECT * INTO existing
      FROM public.platform_saas_payment_allocations_v2
      WHERE attempt_id=p_attempt;
      IF existing.id IS NOT NULL THEN
        IF existing.bank_reference IS DISTINCT FROM btrim(p_reference) THEN
          RAISE EXCEPTION 'Conflicting receipt replay'
            USING ERRCODE='23505';
        END IF;
        RETURN existing.id;
      END IF;
      IF NOT EXISTS(
        SELECT 1 FROM public.platform_saas_payment_proofs_v2
        WHERE attempt_id=p_attempt
      ) OR EXISTS(
        SELECT 1 FROM public.platform_saas_payment_rejections_v2
        WHERE attempt_id=p_attempt
      ) THEN
        RAISE EXCEPTION 'Proof missing or payment rejected'
          USING ERRCODE='42501';
      END IF;
      SELECT coalesce(sum(amount_rub),0) INTO used
      FROM public.platform_saas_receipts
      WHERE invoice_id=invoice.id;
      SELECT used+coalesce(sum(allocated_rub),0) INTO used
      FROM public.platform_saas_payment_allocations_v2
      WHERE invoice_id=invoice.id;
      IF used+original.amount_rub>invoice.total_rub THEN
        RAISE EXCEPTION 'Partial payment would over-allocate invoice'
          USING ERRCODE='23514';
      END IF;
      allocated_id:=gen_random_uuid();
      INSERT INTO public.platform_saas_payment_allocations_v2(
        id,attempt_id,invoice_id,received_currency,received_amount,
        allocated_rub,bank_reference,confirmed_by
      ) VALUES(
        allocated_id,p_attempt,invoice.id,original.currency_code,
        original.amount_currency,original.amount_rub,
        btrim(p_reference),actor
      );
      RETURN allocated_id;
    END; $f$
    """)
    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_reject_attempt(
      p_attempt uuid,p_reason text
    ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; entry record; prior uuid; rejected_id uuid;
    BEGIN
      actor:=public.platform_saas_require_owner();
      IF p_attempt IS NULL OR
         length(btrim(coalesce(p_reason,''))) NOT BETWEEN 5 AND 500 THEN
        RAISE EXCEPTION 'Rejection reason required'
          USING ERRCODE='22023';
      END IF;
      SELECT * INTO entry FROM public.platform_saas_payment_attempts_v2
      WHERE id=p_attempt FOR UPDATE;
      IF entry.id IS NULL OR EXISTS(
        SELECT 1 FROM public.platform_saas_payment_allocations_v2
        WHERE attempt_id=p_attempt
      ) THEN
        RAISE EXCEPTION 'Already settled or unknown payment'
          USING ERRCODE='42501';
      END IF;
      SELECT id INTO prior FROM public.platform_saas_payment_rejections_v2
      WHERE attempt_id=p_attempt;
      IF prior IS NOT NULL THEN RETURN prior; END IF;
      rejected_id:=gen_random_uuid();
      INSERT INTO public.platform_saas_payment_rejections_v2(
        id,attempt_id,reason,rejected_by
      ) VALUES(rejected_id,p_attempt,btrim(p_reason),actor);
      RETURN rejected_id;
    END; $f$
    """)
    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_pending_proofs(
      p_page integer
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE pending jsonb;
    BEGIN
      PERFORM public.platform_saas_require_owner();
      IF p_page IS NULL OR p_page NOT BETWEEN 0 AND 100000 THEN
        RAISE EXCEPTION 'Invalid review page' USING ERRCODE='22023';
      END IF;
      SELECT coalesce(jsonb_agg(jsonb_build_object(
        'id',a.id,'invoice_id',a.invoice_id,
        'owner_user_id',a.owner_user_id,
        'rub',a.amount_rub::text,'currency',a.currency_code,
        'amount',a.amount_currency::text,
        'quote_date',a.quote_date,
        'source_bot',p.source_bot_id,
        'source_message',p.source_message_id,
        'source_file',p.source_file_id,'file_type',p.file_type,
        'submitted_at',p.submitted_at
      ) ORDER BY p.submitted_at,a.id),'[]'::jsonb) INTO pending
      FROM (
        SELECT a.* FROM public.platform_saas_payment_attempts_v2 a
        JOIN public.platform_saas_payment_proofs_v2 p
          ON p.attempt_id=a.id
        WHERE NOT EXISTS(
          SELECT 1 FROM public.platform_saas_payment_allocations_v2 ok
          WHERE ok.attempt_id=a.id
        ) AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_payment_rejections_v2 no
          WHERE no.attempt_id=a.id
        )
        ORDER BY p.submitted_at,a.id LIMIT 8 OFFSET p_page*8
      ) a
      JOIN public.platform_saas_payment_proofs_v2 p ON p.attempt_id=a.id;
      RETURN jsonb_build_object('items',pending,'page',p_page);
    END; $f$
    """)
    # A legacy one-shot full receipt must NEVER follow any partial
    # allocation; conversely, operator approval locks and checks legacy
    # receipts before recording the new allocation.
    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_legacy_guard()
    RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    BEGIN
      IF EXISTS(
        SELECT 1 FROM public.platform_saas_payment_allocations_v2 a
        WHERE a.invoice_id=NEW.invoice_id
      ) THEN
        RAISE EXCEPTION 'Partial allocations already exist'
          USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $f$
    """)
    op.execute("""
    CREATE TRIGGER trg_saas_v2_legacy_guard
    BEFORE INSERT ON public.platform_saas_receipts
    FOR EACH ROW EXECUTE FUNCTION public.platform_saas_v2_legacy_guard()
    """)
    for signature in (
      "platform_saas_v2_bank_method(text,text)",
      "platform_saas_v2_invoice_balance(uuid)",
      "platform_saas_v2_create_attempt(uuid,numeric,text)",
      "platform_saas_v2_submit_proof(uuid,bigint,bigint,text,text)",
      "platform_saas_v2_approve_attempt(uuid,text)",
      "platform_saas_v2_reject_attempt(uuid,text)",
      "platform_saas_v2_pending_proofs(integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"""
          DO $grant$
          BEGIN
            IF EXISTS(SELECT 1 FROM pg_roles
              WHERE rolname='membership_runtime') THEN
              GRANT EXECUTE ON FUNCTION public.{signature}
                TO membership_runtime;
            END IF;
          END $grant$
        """)

    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_available_methods(
      p_invoice uuid
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; result jsonb; original record;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      SELECT * INTO original FROM public.platform_saas_invoices
      WHERE id=p_invoice;
      IF original.id IS NULL OR original.status<>'issued'
         OR original.owner_user_id IS DISTINCT FROM actor THEN
        RAISE EXCEPTION 'Invoice not owned by this account'
          USING ERRCODE='42501';
      END IF;
      SELECT coalesce(jsonb_agg(x.currency_code ORDER BY x.currency_code),
        '[]'::jsonb) INTO result
      FROM (
        SELECT currency_code FROM public.platform_saas_payment_methods_v2
        WHERE enabled=true
        UNION
        SELECT 'RUB'::varchar(3)
        WHERE EXISTS(SELECT 1 FROM public.platform_saas_payment_details
          WHERE id=1 AND instructions IS NOT NULL)
      ) x;
      RETURN result;
    END; $f$
    """)
    op.execute("""
       REVOKE ALL ON FUNCTION
       public.platform_saas_v2_available_methods(uuid) FROM PUBLIC
    """)
    op.execute("""
       DO $grant$
       BEGIN
         IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
         THEN
           GRANT EXECUTE ON FUNCTION
             public.platform_saas_v2_available_methods(uuid)
           TO membership_runtime;
         END IF;
       END $grant$
    """)

    op.execute("""
    CREATE FUNCTION public.platform_saas_v2_methods_read()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE output jsonb;
    BEGIN
      PERFORM public.platform_saas_require_owner();
      SELECT coalesce(jsonb_agg(
        jsonb_build_object(
          'currency',currency_code,'enabled',enabled,
          'instructions',instructions,'updated_at',updated_at
        ) ORDER BY currency_code
      ),'[]'::jsonb) INTO output
      FROM public.platform_saas_payment_methods_v2;
      RETURN output;
    END; $f$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
      public.platform_saas_v2_methods_read() FROM PUBLIC
    """)
    op.execute("""
      DO $grant$
      BEGIN
        IF EXISTS(SELECT 1 FROM pg_roles
          WHERE rolname='membership_runtime') THEN
          GRANT EXECUTE ON FUNCTION
            public.platform_saas_v2_methods_read()
          TO membership_runtime;
        END IF;
      END $grant$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Do not roll back append-only ViMi Pay payment evidence"
    )
