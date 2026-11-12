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


def downgrade() -> None:
    raise RuntimeError(
        "Do not roll back append-only ViMi Pay payment evidence"
    )
