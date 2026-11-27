"""Attach the authorized bot username to an immutable Stars checkout quote.

Revision ID: 0109_stars_checkout_context
Revises: 0108_native_departure_outbox
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0109_stars_checkout_context"
down_revision: str | None = "0108_native_departure_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE FUNCTION public.platform_saas_stars_create_checkout(
  p_invoice uuid, p_telegram_id bigint, p_central_bot bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE quote jsonb; bot_username text; actor uuid;
BEGIN
  -- The original function authorizes the liable payer and freezes the
  -- exact debt, currency and expiry. It remains the only order creator.
  quote := public.platform_saas_stars_create_order(
    p_invoice, p_telegram_id, p_central_bot);
  IF quote IS NULL OR jsonb_typeof(quote) <> 'object' THEN
    RAISE EXCEPTION 'Invalid Stars checkout quote' USING ERRCODE='22023';
  END IF;
  actor := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  SELECT b.username INTO bot_username
    FROM public.platform_saas_invoices i
    JOIN public.bot_instances b ON b.id=i.bot_instance_id
    JOIN public.projects p ON p.id=b.project_id
    WHERE i.id=p_invoice AND i.owner_user_id=actor
      AND p.owner_user_id=actor;
  -- Retired bots have no live username. Do not guess from another bot.
  RETURN quote || jsonb_build_object('bot_username',bot_username);
END; $f$
    """)
    op.execute("""
REVOKE ALL ON FUNCTION
  public.platform_saas_stars_create_checkout(uuid,bigint,bigint) FROM PUBLIC
    """)
    op.execute("""
DO $r$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
    GRANT EXECUTE ON FUNCTION
      public.platform_saas_stars_create_checkout(uuid,bigint,bigint)
      TO membership_runtime;
  END IF;
END $r$
    """)


def downgrade() -> None:
    op.execute("""
DROP FUNCTION public.platform_saas_stars_create_checkout(uuid,bigint,bigint)
    """)
