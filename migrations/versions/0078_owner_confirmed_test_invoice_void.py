"""Cancel one explicitly owner-confirmed test SaaS invoice safely.

Revision ID: 0078_saas_test_void
Revises: 0077_saas_transition

An operator confirmed the historic November 13 test invoice `f09e1fce-4585-494b-8527-312a6051d3fa`
is not a genuine sale or received payment. The first-rollout correction
left it open because an immutable payment attempt might exist. This
single-invoice, fail-closed repair preserves the original ledger, any test
payment attempts, and the migration 0077 audit trail. It REFUSES to void if
there is a submitted proof, any settled amount, FX settlement, or credit.
No automatic cleanup of any other invoice or commercial activation.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0078_saas_test_void"
down_revision: str | None = "0077_saas_transition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A test quote may remain after its invoice is marked VOID. The
    # original proof handler did not check invoice state. Close that
    # narrow hole before the repair, without changing existing callbacks.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v2_submit_proof(
      p_attempt uuid,p_bot bigint,p_chat bigint,p_message bigint,
      p_file_id text,p_kind text
    ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; original record; existing uuid; proof uuid;
            invoice_status text;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      -- Both proof submission and verified-refund allocation use the same
      -- advisory lock before checking quote expiry or credit eligibility.
      -- An expired quote cannot race an uncommitted genuine payment proof.
      PERFORM pg_advisory_xact_lock(
        hashtextextended('vimi:saas:verified-refunds',0)
      );
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
      -- A quoted bank transfer may have been cancelled after issue.
      -- Invoice state must be checked under the same lock order as the
      -- payment approval path (attempt -> invoice). No orphan proof can
      -- be submitted after its invoice was voided.
      SELECT status INTO invoice_status
      FROM public.platform_saas_invoices
      WHERE id=original.invoice_id FOR UPDATE;
      IF invoice_status IS DISTINCT FROM 'issued' THEN
        RAISE EXCEPTION 'Invoice has been voided or is unavailable'
          USING ERRCODE='42501';
      END IF;
      SELECT id INTO existing FROM public.platform_saas_payment_proofs_v2
      WHERE attempt_id=p_attempt;
      IF existing IS NOT NULL THEN RETURN existing; END IF;
      proof:=gen_random_uuid();
      INSERT INTO public.platform_saas_payment_proofs_v2(
        id,attempt_id,source_bot_id,source_chat_id,
        source_message_id,source_file_id,file_type
      ) VALUES(
        proof,p_attempt,p_bot,p_chat,p_message,p_file_id,p_kind
      );
      RETURN proof;
    END; $f$
    """)
    op.execute("""
      DO $cleanup$
      DECLARE
        bill record;
        grace_end timestamptz;
      BEGIN
        -- Same serialization as the monthly issuer and refund sweeper.
        PERFORM pg_advisory_xact_lock(
          hashtextextended('vimi:saas:due-reconcile',0)
        );
        PERFORM pg_advisory_xact_lock(
          hashtextextended('vimi:saas:verified-refunds',0)
        );
        SELECT * INTO bill
        FROM public.platform_saas_invoices
        WHERE id='f09e1fce-4585-494b-8527-312a6051d3fa'::uuid
        FOR UPDATE;
        IF NOT FOUND THEN
          -- Empty migrated CI DB, never manufacture a target invoice.
          RAISE NOTICE 'Specific historic test invoice not present; no-op';
          RETURN;
        END IF;
        IF bill.status='void' THEN
          RAISE NOTICE 'Specific historic test invoice already void';
          RETURN;
        END IF;
        IF bill.status<>'issued'
          OR bill.kind<>'prepayment'
          OR bill.owner_user_id<>'f61a7b70-f0ee-47a7-a83e-df68fd357235'::uuid
          OR bill.merchant_id<>'8c92d3e3-4eba-4258-bec3-817ec7a3f32c'::uuid
          OR bill.total_rub<>150.00
          OR (bill.due_at AT TIME ZONE 'Europe/Moscow')::date
                <> DATE '2026-11-13'
          OR NOT public.platform_saas_is_transition_owner(bill.owner_user_id)
        THEN
          RAISE EXCEPTION 'Refusing unrelated or already changed invoice'
            USING ERRCODE='42501';
        END IF;
        grace_end:=public.platform_saas_billable_after(bill.owner_user_id);
        IF grace_end IS NULL OR bill.period_start>=grace_end THEN
          RAISE EXCEPTION 'Test invoice does not qualify for trial correction'
            USING ERRCODE='42501';
        END IF;
        IF EXISTS(
            SELECT 1 FROM public.platform_saas_receipts r
            WHERE r.invoice_id=bill.id
          ) OR EXISTS(
            SELECT 1 FROM public.platform_saas_payment_allocations_v2 a
            WHERE a.invoice_id=bill.id
          ) OR EXISTS(
            SELECT 1 FROM public.platform_saas_payment_proofs_v2 proof
            JOIN public.platform_saas_payment_attempts_v2 attempt
              ON attempt.id=proof.attempt_id
            WHERE attempt.invoice_id=bill.id
          ) OR EXISTS(
            SELECT 1 FROM public.platform_saas_currency_settlements c
            WHERE c.invoice_id=bill.id
          ) OR EXISTS(
            SELECT 1 FROM public.platform_saas_verified_credit_allocations c
            WHERE c.invoice_id=bill.id
          ) OR EXISTS(
            SELECT 1 FROM public.platform_saas_verified_credit_claims c
            JOIN public.platform_saas_invoice_lines l
              ON l.id=c.invoice_line_id
            WHERE l.invoice_id=bill.id
          ) OR EXISTS(
            SELECT 1 FROM public.platform_saas_refund_credits c
            JOIN public.platform_saas_invoice_lines l
              ON l.id=c.invoice_line_id
            WHERE l.invoice_id=bill.id
          )
        THEN
          -- An unproven attempt/quote is safe to retire; any money,
          -- submitted proof or compensation requires manual review.
          RAISE EXCEPTION
            'Test invoice has real payment/proof or financial evidence'
            USING ERRCODE='42501';
        END IF;
        UPDATE public.platform_saas_invoices
        SET status='void'
        WHERE id=bill.id AND status='issued';
        IF NOT FOUND THEN
          RAISE EXCEPTION 'Concurrent invoice change prevented repair'
            USING ERRCODE='40001';
        END IF;
        INSERT INTO public.platform_saas_trial_transition_void_audit(
          invoice_id,owner_user_id,old_due_at,grace_ends_at,
          old_total_rub,reason
        ) VALUES(
          bill.id,bill.owner_user_id,bill.due_at,grace_end,
          bill.total_rub,'owner_confirmed_unpaid_test_invoice'
        );
        RAISE NOTICE 'Voided only the operator-confirmed 150 RUB test invoice';
      END $cleanup$
    """)


def downgrade() -> None:
    raise RuntimeError("Never restore an audited voided financial invoice")
