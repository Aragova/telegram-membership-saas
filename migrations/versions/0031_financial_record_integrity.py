"""Protect tenant payment evidence against update/delete/reset.

Revision ID: 0031_financial_record_integrity
Revises: 0030_platform_risk_review

The owning project can still create/submit/approve/reject/cancel requests
and renew subscriptions. Committed Payment facts and the original request
pricing/customer snapshot cannot be rewritten or removed by a tenant, even
through a future incorrect API/SQL path. No billing/fees/trials activated.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0031_financial_record_integrity"
down_revision: str | None = "0030_platform_risk_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.vimi_guard_financial_record()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $guard$
        BEGIN
            IF TG_TABLE_NAME = 'payments' THEN
                -- A Payment exists ONLY after final tenant approval.
                -- Deleting it (even via project cascade) or mutating an
                -- amount, owner, approval timestamp or original currency
                -- would falsify the source of financial history.
                RAISE EXCEPTION
                    'Confirmed payment records cannot be changed or deleted'
                    USING ERRCODE = 'P6901';
            ELSIF TG_TABLE_NAME = 'payment_requests' THEN
                IF TG_OP = 'DELETE' THEN
                    -- Even rejected/expired requests are a durable record
                    -- of prior checkout attempts, not disposable config.
                    RAISE EXCEPTION
                        'Payment request history cannot be deleted'
                        USING ERRCODE = 'P6902';
                END IF;
                IF TG_OP <> 'UPDATE' THEN
                    RAISE EXCEPTION 'Invalid financial mutation'
                        USING ERRCODE = '23514';
                END IF;
                -- Freeze the price/currency/recipient/term at checkout.
                -- Only state and its review timestamps may change during
                -- the approved existing business flow.
                IF ROW(
                    NEW.id, NEW.project_id, NEW.customer_id, NEW.plan_id,
                    NEW.plan_price_id, NEW.payment_method_id,
                    NEW.currency_code, NEW.regular_amount,
                    NEW.discount_amount, NEW.final_amount,
                    NEW.promotion_id, NEW.duration_value, NEW.duration_unit,
                    NEW.created_at
                ) IS DISTINCT FROM ROW(
                    OLD.id, OLD.project_id, OLD.customer_id, OLD.plan_id,
                    OLD.plan_price_id, OLD.payment_method_id,
                    OLD.currency_code, OLD.regular_amount,
                    OLD.discount_amount, OLD.final_amount,
                    OLD.promotion_id, OLD.duration_value, OLD.duration_unit,
                    OLD.created_at
                ) THEN
                    RAISE EXCEPTION
                        'Payment request financial snapshot cannot be changed'
                        USING ERRCODE = 'P6903';
                END IF;
                IF OLD.status IN (
                    'approved', 'rejected', 'cancelled', 'expired'
                ) AND (
                    NEW.status IS DISTINCT FROM OLD.status
                    OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at
                    OR NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at
                    OR NEW.reviewed_by IS DISTINCT FROM OLD.reviewed_by
                ) THEN
                    RAISE EXCEPTION
                        'Final payment request decision cannot be changed'
                        USING ERRCODE = 'P6904';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Unexpected financial table'
                USING ERRCODE = '23514';
        END
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_vimi_payment_facts_immutable
        BEFORE UPDATE OR DELETE ON public.payments
        FOR EACH ROW EXECUTE FUNCTION public.vimi_guard_financial_record()
    """)
    op.execute("""
        CREATE TRIGGER trg_vimi_request_history_immutable
        BEFORE UPDATE OR DELETE ON public.payment_requests
        FOR EACH ROW EXECUTE FUNCTION public.vimi_guard_financial_record()
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION public.vimi_guard_financial_record() FROM PUBLIC
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS trg_vimi_request_history_immutable
        ON public.payment_requests
    """)
    op.execute("""
        DROP TRIGGER IF EXISTS trg_vimi_payment_facts_immutable
        ON public.payments
    """)
    op.execute("DROP FUNCTION IF EXISTS public.vimi_guard_financial_record()")
