"""Remove PR #158 test-invoice cancellation; preserve Alembic and audit history.

Revision ID: 0082_saas_remove_test_void
Revises: 0081_saas_test_invoice_void

#158's migration has already run on staging, so deleting or rewriting 0081
would break subsequent migrations. This forward-only cleanup removes the
obsolete action. Its audit table is dropped only if it contains no entries;
historical audit rows, if any, must never be erased.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0082_saas_remove_test_void"
down_revision: str | None = "0081_saas_test_invoice_void"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Revoke the unnecessary capability for every role by dropping the RPC.
    # Do not modify invoices, allocations, payment attempts or proofs.
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_void_test_prepayment(uuid,uuid,numeric)"
    )
    op.execute("""
    DO $cleanup$
    BEGIN
      IF to_regclass(
        'public.platform_saas_test_invoice_void_audit'
      ) IS NOT NULL THEN
        -- This table has FORCE ROW LEVEL SECURITY with no read policy.
        -- Ordinary SELECT would appear empty even if audit rows exist.
        -- Hold an exclusive lock, let the migration table owner inspect
        -- all rows, then restore FORCE RLS when preserving audit history.
        LOCK TABLE public.platform_saas_test_invoice_void_audit
          IN ACCESS EXCLUSIVE MODE;
        ALTER TABLE public.platform_saas_test_invoice_void_audit
          NO FORCE ROW LEVEL SECURITY;
        IF EXISTS(
          SELECT 1 FROM public.platform_saas_test_invoice_void_audit
        ) THEN
          ALTER TABLE public.platform_saas_test_invoice_void_audit
            FORCE ROW LEVEL SECURITY;
          RAISE NOTICE 'Retaining existing invoice-void audit history';
        ELSE
          DROP TABLE public.platform_saas_test_invoice_void_audit;
        END IF;
      END IF;
    END;
    $cleanup$
    """)


def downgrade() -> None:
    # Recreating a discontinued financial operation is prohibited.
    raise RuntimeError("Removed invoice-void feature cannot be restored")
