"""Isolated, read-only short checkout redirect for existing Lava orders.

Revision ID: 0097_saas_payment_shortlinks
Revises: 0096_native_buyer_hold_guard

No new payment records, invoices, webhooks, provider calls or privileges for
the public API. An opaque UUIDv4 is already assigned to each gateway order.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0097_saas_payment_shortlinks"
down_revision: str | None = "0096_native_buyer_hold_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOLOGIN is a privilege group, never an application credential.
    # A separately provisioned LOGIN can inherit *only* this narrow role.
    op.execute("""
DO $r$ BEGIN
  IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_shortlink_reader') THEN
    CREATE ROLE membership_shortlink_reader NOLOGIN NOSUPERUSER
      NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $r$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO membership_shortlink_reader")
    op.execute("""
CREATE FUNCTION public.platform_saas_lava_shortlink_target(p_order uuid)
RETURNS text LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $f$
  SELECT o.provider_invoice_url
  FROM public.platform_saas_gateway_orders o
  JOIN public.platform_saas_invoices i ON i.id=o.invoice_id
  WHERE o.id=p_order
    AND o.provider='lava'
    AND o.status='issued'
    AND o.expires_at>clock_timestamp()
    AND i.status='issued'
    AND i.owner_user_id=o.owner_user_id
    -- Do not redirect a payer to a stale quote after a different rail
    -- settled/partially settled the original bill.
    AND o.amount_rub=public.platform_saas_v2_remaining(i.id)
    AND NOT EXISTS(
      SELECT 1 FROM public.platform_saas_gateway_charges c WHERE c.order_id=o.id
    )
    -- Retired original bots retain historical debt; transferred live bots
    -- must never redirect the former owner into an ordinary settlement.
    AND (
      i.bot_instance_id IS NULL
      OR NOT EXISTS(
        SELECT 1 FROM public.bot_instances b WHERE b.id=i.bot_instance_id
      )
      OR EXISTS(
        SELECT 1 FROM public.bot_instances b
        JOIN public.projects p ON p.id=b.project_id
        WHERE b.id=i.bot_instance_id AND p.owner_user_id=i.owner_user_id
      )
    )
  LIMIT 1
$f$
    """)
    # No public execution, no direct SELECT on RLS-protected finance tables,
    # and in particular no gateway bind/charge/refund rights for this role.
    op.execute("""
REVOKE ALL ON FUNCTION public.platform_saas_lava_shortlink_target(uuid)
FROM PUBLIC
    """)
    op.execute("""
GRANT EXECUTE ON FUNCTION public.platform_saas_lava_shortlink_target(uuid)
TO membership_shortlink_reader
    """)


def downgrade() -> None:
    op.execute("""
REVOKE ALL ON FUNCTION public.platform_saas_lava_shortlink_target(uuid)
FROM membership_shortlink_reader
    """)
    op.execute("DROP FUNCTION public.platform_saas_lava_shortlink_target(uuid)")
    # Keep the harmless NOLOGIN role: an operator may have granted it to an
    # independently provisioned login. Do not break that ownership on rollback.
