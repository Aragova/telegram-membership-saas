"""Real PostgreSQL access check and pure rendering tests for trial UX.

The DB checks use a rollback-only transaction. No invented invoice, purchase,
trial activation or separate button is committed to a live network.
"""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.premium_welcome import (
    central_welcome_classic,
    central_welcome_rich,
    trial_status_classic,
    trial_status_rich,
)
from test_platform_saas_invoice_rls import _project, _tid


def test_premium_onboarding_uses_native_rich_and_exact_same_buttons():
    rich = central_welcome_rich()
    classic = central_welcome_classic()
    assert "<h3>✨ ViMi Pay</h3>" in rich
    assert "<table bordered compact>" in rich
    assert "72" in rich and "5%" in rich
    assert "72" in classic and "5%" in classic
    # No obsolete callback labels, trial activation command, or test UI.
    assert "callback_data" not in rich
    assert "activate" not in classic.lower()


def test_trial_messages_only_show_real_recorded_deadlines():
    at = datetime.now(UTC) + timedelta(hours=70)
    active = {
        "state": "trial",
        "remaining_seconds": 70 * 3600,
        "trial_ends_at": at.isoformat(),
    }
    rendered = trial_status_classic(active, timezone="Europe/Moscow")
    assert "70" in rendered or "2 дн. 22 ч." in rendered
    assert "tg-time" in rendered or "UTC+03:00" in rendered
    assert "<h3>" in trial_status_rich(active, timezone="Europe/Moscow")
    missing = trial_status_classic(
        {"state": "pending", "trial_ends_at": None}, timezone="Europe/Moscow"
    )
    assert "tg-time" not in missing
    assert "72 часа" not in missing


@pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL owner-scoped billing status",
)
async def test_account_trial_status_from_immutable_first_bot_claim():
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    try:
        owner, merchant, _, _, _ = await _project(factory, _tid())
        now = datetime.now(UTC)
        trial_started = now - timedelta(hours=2)
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(
                    text("SELECT set_config('app.current_user_id',:owner,true)"),
                    {"owner": str(owner)},
                )
                await conn.execute(text(
                    "UPDATE platform_saas_billing_control "
                    "SET enabled=true,effective_from=:from_time WHERE id=1"
                ), {"from_time": now - timedelta(days=1)})
                await conn.execute(text(
                    "INSERT INTO merchant_platform_trial_claims "
                    "(id,merchant_id,owner_user_id,policy_key,scope,claimed_at) "
                    "VALUES (:id,:merchant,:owner,'vimi-saas-72h-v1','owner',:at)"
                ), {
                    "id": uuid4(), "merchant": merchant, "owner": owner,
                    "at": trial_started,
                })
                q = text(
                    "SELECT public.platform_saas_owner_trial_status() AS data"
                ).columns(data=JSONB)
                status = (await conn.execute(q)).scalar_one()
                assert status["state"] == "trial"
                end = datetime.fromisoformat(status["trial_ends_at"])
                assert end == trial_started + timedelta(hours=72)
                assert 69 * 3600 <= status["remaining_seconds"] <= 70 * 3600
                await conn.execute(text(
                    "UPDATE platform_saas_billing_control "
                    "SET enabled=false,effective_from=NULL WHERE id=1"
                ))
                disabled = (await conn.execute(q)).scalar_one()
                assert disabled["state"] == "disabled"
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()
