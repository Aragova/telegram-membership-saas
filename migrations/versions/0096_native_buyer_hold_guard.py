"""Narrow, buyer-session-safe native Telegram Stars commerce hold gate.

Buyer ingress intentionally has no app.current_user_id. The owner-only
platform_saas_project_commerce_state RPC therefore MUST NOT be called by
the storefront. Return only a boolean scoped to the trusted project+bot
context, without exposing invoice, debt, owner or merchant identifiers.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0096_native_buyer_hold_guard"
down_revision: str | None = "0095_native_channel_stars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(r"""
CREATE FUNCTION public.platform_saas_native_buyer_sales_allowed(
    p_project uuid, p_telegram_bot_id bigint
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE
    scoped uuid;
    source_bot uuid;
BEGIN
    scoped := NULLIF(
      current_setting('app.current_project_id',true),''
    )::uuid;
    IF p_project IS NULL OR p_telegram_bot_id IS NULL
       OR scoped IS DISTINCT FROM p_project THEN
        RETURN false;
    END IF;
    SELECT b.id INTO source_bot
    FROM public.bot_instances b
    JOIN public.projects p ON p.id=b.project_id
    WHERE b.project_id=p_project
      AND b.telegram_bot_id=p_telegram_bot_id
      AND b.status='registered'
      AND p.status='active';
    IF source_bot IS NULL THEN RETURN false; END IF;
    RETURN public.platform_saas_bot_hold_state(
      source_bot,clock_timestamp()
    ) IN ('clear','disabled');
END; $f$;
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
          public.platform_saas_native_buyer_sales_allowed(uuid,bigint)
        FROM PUBLIC
    """)
    op.execute("""
        DO $grant$ BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_native_buyer_sales_allowed(uuid,bigint)
              TO membership_runtime;
          END IF;
        END $grant$
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS
          public.platform_saas_native_buyer_sales_allowed(uuid,bigint)
    """)
