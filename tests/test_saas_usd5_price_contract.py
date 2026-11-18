"""USD 5 future service price, with immutable USD 3 legacy obligations.

These tests do not issue real platform bills or mutate existing balances.
"""
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory

ROOT = Path(__file__).resolve().parents[1]


def test_forward_only_usd5_migration_keeps_legacy_usd3_valid() -> None:
    migration = (ROOT / "migrations/versions/0091_saas_usd5.py").read_text(
        encoding="utf-8",
    )
    assert 'down_revision: str | None = "0090_free_file_products"' in migration
    assert "amount_native:=5;" in migration
    assert "invoice_rub:=round(5*usd_rate,2);" in migration
    assert "'monthly_amount',CASE WHEN currency='USD' THEN '5.00'" in migration
    assert "NEW.original_amount IN (3,5)" in migration
    assert "round(NEW.original_amount*bot_fee_usd_fx,2)" in migration
    assert "UPDATE public.platform_saas_invoices" in migration
    # The UPDATE exists inside original issuance logic, not as a bulk
    # rewrite of historical invoices.
    assert "SET total_rub=" not in migration
    assert "DELETE FROM public.platform_saas_invoices" not in migration
    assert "DELETE FROM public.platform_saas_payment_allocations" not in migration


def test_new_price_in_both_owner_languages() -> None:
    ui = (ROOT / "src/telegram_membership_saas/modules/owner_setup/handlers.py").read_text(
        encoding="utf-8",
    )
    # Price is still 150 RUB / USD 5; navigation belongs to Bill, not Settings.
    assert "🇷🇺 150 ₽" in ui
    assert "🇺🇸 $5" in ui
    assert '"saasprice:USD"' in ui
    assert '"saasprice:RUB"' in ui
    assert "Валюта обслуживания ViMi Pay" in ui
    assert "ViMi Pay service currency" in ui
    assert '"saasinvoices:0"' in ui


@pytest.mark.skipif(
    not (os.getenv("TMS_MIGRATION_DATABASE_URL") and os.getenv("TMS_DATABASE_URL")),
    reason="Requires migrated PostgreSQL and runtime tenant role",
)
async def test_usd_service_profile_reads_new_five_dollar_price():
    settings = Settings()
    runtime = build_engine(settings)
    factory = build_session_factory(runtime)
    admin = create_async_engine(settings.migration_database_url)
    try:
        owner, _, _, _, _ = await _project(factory, _tid())
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO public.platform_saas_price_profiles"
                    "(owner_user_id,currency_code) VALUES(:owner,'USD')"
                ), {"owner": owner})
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:owner,true)"
                ), {"owner": str(owner)})
                data = (await conn.execute(text(
                    "SELECT public.platform_saas_price_profile_read() AS data"
                ).columns(data=JSONB))).scalar_one()
                assert data["currency"] == "USD"
                assert data["monthly_amount"] == "5.00"
            finally:
                await tx.rollback()
    finally:
        await runtime.dispose()
        await admin.dispose()
