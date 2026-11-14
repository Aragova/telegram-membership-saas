"""Audit-safe operator cancellation for confirmed unpaid *test* prepayments.

Revision ID: 0081_saas_test_invoice_void
Revises: 0080_saas_proof_view

The central owner can void a specifically reviewed 150 RUB test prepayment
ONLY while monetization is disabled, with no actual financial allocations,
receipts, settlements, credits or unsettled proofs. All rejected documents
and financial snapshots remain unchanged. Every void leaves an audit record.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081_saas_test_invoice_void"
down_revision: str | None = "0080_saas_proof_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_saas_test_invoice_void_audit",
        sa.Column(
            "invoice_id", sa.Uuid(),
            sa.ForeignKey("platform_saas_invoices.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "operator_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount_rub", sa.Numeric(38, 2), nullable=False),
        sa.Column("rejected_proofs", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column(
            "voided_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "reason='operator_confirmed_unpaid_test_prepayment'",
            name="ck_saas_test_void_reason",
        ),
        sa.CheckConstraint(
            "amount_rub=150 AND rejected_proofs>=0",
            name="ck_saas_test_void_snapshot",
        ),
    )
    op.execute(
        "ALTER TABLE public.platform_saas_test_invoice_void_audit "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_test_invoice_void_audit "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "REVOKE ALL ON public.platform_saas_test_invoice_void_audit "
        "FROM PUBLIC"
    )
    op.execute("""
    CREATE FUNCTION public.platform_saas_void_test_prepayment(
      p_invoice uuid,p_expected_owner uuid,p_expected_amount numeric
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; original record; rejected_count integer;
    BEGIN
      -- The SQL operator role is checked *again* regardless of Telegram UI.
      actor:=public.platform_saas_require_owner();
      IF p_invoice IS NULL OR p_expected_owner IS NULL
         OR p_expected_amount IS DISTINCT FROM 150.00 THEN
        RAISE EXCEPTION 'Invalid test invoice confirmation'
          USING ERRCODE='22023';
      END IF;
      -- Serialize with platform_saas_release_change(arm/stop), which
      -- uses the same pilot-control lock. Keep it until transaction commit,
      -- so live billing cannot activate between check and cancellation.
      PERFORM pg_advisory_xact_lock(
        hashtextextended('vimi:saas:pilot-control',0)
      );
      -- Do not expose generic invoice cancellations during active billing.
      -- This operation is solely a pre-release test-account cleanup.
      IF EXISTS(
        SELECT 1 FROM public.platform_saas_billing_control b
        WHERE b.id=1 AND b.enabled
      ) THEN
        RAISE EXCEPTION 'Test invoice cleanup disabled during live billing'
          USING ERRCODE='42501';
      END IF;
      -- Use established financial reconciliation order.
      PERFORM pg_advisory_xact_lock(
        hashtextextended('vimi:saas:due-reconcile',0)
      );
      PERFORM pg_advisory_xact_lock(
        hashtextextended('vimi:saas:verified-refunds',0)
      );
      SELECT * INTO original FROM public.platform_saas_invoices
        WHERE id=p_invoice FOR UPDATE;
      IF NOT FOUND OR original.status<>'issued'
         OR original.kind<>'prepayment'
         OR original.total_rub<>150.00
         OR original.owner_user_id IS DISTINCT FROM p_expected_owner
         OR original.bot_instance_id IS NULL THEN
        RAISE EXCEPTION 'Only the reviewed unpaid test prepayment can be voided'
          USING ERRCODE='42501';
      END IF;
      -- Reject any confirmed money or credit. Never erase payment evidence.
      IF EXISTS(
        SELECT 1 FROM public.platform_saas_receipts r
        WHERE r.invoice_id=original.id
      ) OR EXISTS(
        SELECT 1 FROM public.platform_saas_payment_allocations_v2 a
        WHERE a.invoice_id=original.id
      ) OR EXISTS(
        SELECT 1 FROM public.platform_saas_currency_receipts r
        WHERE r.invoice_id=original.id
      ) OR EXISTS(
        SELECT 1 FROM public.platform_saas_currency_settlements s
        WHERE s.invoice_id=original.id
      ) OR EXISTS(
        SELECT 1 FROM public.platform_saas_verified_credit_allocations a
        WHERE a.invoice_id=original.id
      ) OR EXISTS(
        SELECT 1 FROM public.platform_saas_verified_credit_claims c
        JOIN public.platform_saas_invoice_lines l
          ON l.id=c.invoice_line_id
        WHERE l.invoice_id=original.id
      ) OR EXISTS(
        SELECT 1 FROM public.platform_saas_refund_credits c
        JOIN public.platform_saas_invoice_lines l
          ON l.id=c.invoice_line_id
        WHERE l.invoice_id=original.id
      ) OR EXISTS(
        SELECT 1 FROM public.platform_saas_payment_proofs_v2 p
        JOIN public.platform_saas_payment_attempts_v2 a
          ON a.id=p.attempt_id
        WHERE a.invoice_id=original.id
          AND NOT EXISTS(
            SELECT 1 FROM public.platform_saas_payment_rejections_v2 rejected
            WHERE rejected.attempt_id=p.attempt_id
          )
      ) THEN
        RAISE EXCEPTION 'Payment, credit or unrejected proof prevents void'
          USING ERRCODE='42501';
      END IF;
      SELECT count(*) INTO rejected_count
      FROM public.platform_saas_payment_rejections_v2 rejected
      JOIN public.platform_saas_payment_attempts_v2 a
        ON a.id=rejected.attempt_id
      WHERE a.invoice_id=original.id;
      -- Existing guard and accounting triggers provide a second protection.
      UPDATE public.platform_saas_invoices
      SET status='void'
      WHERE id=original.id AND status='issued';
      IF NOT FOUND THEN
        RAISE EXCEPTION 'Concurrent invoice change prevented cancellation'
          USING ERRCODE='40001';
      END IF;
      INSERT INTO public.platform_saas_test_invoice_void_audit(
        invoice_id,operator_user_id,owner_user_id,amount_rub,
        rejected_proofs,reason
      ) VALUES(
        original.id,actor,original.owner_user_id,original.total_rub,
        rejected_count,'operator_confirmed_unpaid_test_prepayment'
      );
      RETURN jsonb_build_object(
        'invoice_id',original.id,'status','void',
        'amount_rub',original.total_rub::text,
        'rejected_proofs',rejected_count
      );
    END; $f$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_void_test_prepayment(uuid,uuid,numeric) "
        "FROM PUBLIC"
    )
    op.execute("""
      DO $grant$
      BEGIN
        IF EXISTS(
          SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
        ) THEN
          GRANT EXECUTE ON FUNCTION
            public.platform_saas_void_test_prepayment(uuid,uuid,numeric)
          TO membership_runtime;
        END IF;
      END $grant$
    """)


def downgrade() -> None:
    # Never erase financial cancellation audit or resurrect liabilities.
    raise RuntimeError("Test invoice void audit and status are irreversible")
