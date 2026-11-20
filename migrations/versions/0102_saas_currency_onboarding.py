"""Explicit first-time per-bot service currency selection, without repricing history.

Revision ID: 0102_saas_currency_onboarding
Revises: 0101_saas_provider_refunds

Existing registered bots are confirmed. Only newly registered bots require the
explicit last-step confirmation of the inherited safe RUB/USD default.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0102_saas_currency_onboarding"
down_revision: str | None = "0101_saas_provider_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing bots keep their price without re-onboarding or changing
    # any invoice. Future bot profiles have an unconfirmed default.
    op.execute("""
ALTER TABLE public.platform_saas_bot_price_profiles
 ADD COLUMN currency_selected boolean NOT NULL DEFAULT false
    """)
    op.execute("""
UPDATE public.platform_saas_bot_price_profiles SET currency_selected=true
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_bot_currency_selection_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public AS $f$
BEGIN
 NEW.currency_selected:=true;
 RETURN NEW;
END; $f$
    """)
    op.execute("""
CREATE TRIGGER platform_saas_bot_currency_selection_confirmed
 BEFORE UPDATE OF currency_code
 ON public.platform_saas_bot_price_profiles
 FOR EACH ROW EXECUTE FUNCTION
 public.platform_saas_bot_currency_selection_guard()
    """)
    # The existing authenticated reader guards actor and project ownership.
    # No new direct table-read grant is added to membership_runtime.
    op.execute("""
CREATE FUNCTION public.platform_saas_bot_price_profile_read_v2(
 p_project uuid
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE summary jsonb; selected boolean;
BEGIN
 summary:=public.platform_saas_bot_price_profile_read(p_project);
 SELECT bp.currency_selected INTO STRICT selected
 FROM public.platform_saas_bot_price_profiles bp
 WHERE bp.bot_instance_id=(summary->>'bot_id')::uuid;
 RETURN summary || jsonb_build_object('currency_selected',selected);
EXCEPTION WHEN NO_DATA_FOUND OR TOO_MANY_ROWS THEN
 RAISE EXCEPTION 'Invalid bot price profile' USING ERRCODE='42501';
END; $f$
    """)
    op.execute("""
REVOKE ALL ON FUNCTION
 public.platform_saas_bot_price_profile_read_v2(uuid) FROM PUBLIC
    """)
    op.execute("""
DO $r$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
   GRANT EXECUTE ON FUNCTION
    public.platform_saas_bot_price_profile_read_v2(uuid)
   TO membership_runtime;
 END IF;
END $r$
    """)


def downgrade() -> None:
    op.execute("""
DROP FUNCTION IF EXISTS public.platform_saas_bot_price_profile_read_v2(uuid)
    """)
    op.execute("""
DROP TRIGGER IF EXISTS platform_saas_bot_currency_selection_confirmed
 ON public.platform_saas_bot_price_profiles
    """)
    op.execute("""
DROP FUNCTION IF EXISTS public.platform_saas_bot_currency_selection_guard()
    """)
    op.execute("""
ALTER TABLE public.platform_saas_bot_price_profiles
 DROP COLUMN IF EXISTS currency_selected
    """)
