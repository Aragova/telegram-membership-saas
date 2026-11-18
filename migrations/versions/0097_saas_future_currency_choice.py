"""Permit future tariff currency choice without rewriting old unpaid bills.

Revision ID: 0097_saas_future_currency_choice
Revises: 0096_native_buyer_hold_guard

The original restriction rejected a prospective currency change whenever ANY
historical bot invoice in the owner's network was unpaid. Existing invoices
are immutable and independent of the future price profile; debt and commercial
holds continue to apply. Only the prospective settings RPC is modified.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0097_saas_future_currency_choice"
down_revision: str | None = "0096_native_buyer_hold_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _profile_set(*, block_on_unpaid: bool) -> None:
    guard = """
      IF EXISTS(
        SELECT 1 FROM public.platform_saas_invoices i
        WHERE i.owner_user_id=actor AND i.kind='prepayment'
          AND i.status='issued'
          AND public.platform_saas_v2_remaining(i.id)>0
      ) THEN
        RAISE EXCEPTION 'Settle outstanding service invoice before changing profile'
          USING ERRCODE='23514';
      END IF;
    """ if block_on_unpaid else ""
    op.execute("""
    CREATE OR REPLACE FUNCTION public.platform_saas_price_profile_set(p_currency text)
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      IF actor IS NULL OR p_currency NOT IN ('RUB','USD') THEN
        RAISE EXCEPTION 'Invalid account price profile'
          USING ERRCODE='42501';
      END IF;
      PERFORM pg_advisory_xact_lock(hashtextextended(
        'vimi:saas:price:'||actor::text,0
      ));
    """ + guard + """
      INSERT INTO public.platform_saas_price_profiles(
        owner_user_id,currency_code
      ) VALUES(actor,p_currency)
      ON CONFLICT(owner_user_id) DO UPDATE SET
        currency_code=excluded.currency_code,updated_at=clock_timestamp();
      RETURN public.platform_saas_price_profile_read();
    END; $f$
    """)


def upgrade() -> None:
    _profile_set(block_on_unpaid=False)


def downgrade() -> None:
    _profile_set(block_on_unpaid=True)
