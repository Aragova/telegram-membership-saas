"""Regression: installed operator monetization screen and activation must work.

The staging release must not call the creator-preflight function removed in
migration 0050. Activation is exercised only inside a rolled-back transaction.
"""
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import (
    ensure_telegram_actor,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and a rollback-only operator context",
)


async def test_operator_monetization_screen_opens_and_arm_can_be_confirmed():
    cfg = Settings()
    app_engine = build_engine(cfg)
    factory = build_session_factory(app_engine)
    admin = create_async_engine(cfg.migration_database_url)
    try:
        telegram_id = uuid4().int % 7_000_000_000_000_000_000 + 1
        actor = await ensure_telegram_actor(
            factory, telegram_id=telegram_id,
            username=None, first_name=None,
        )
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": telegram_id})
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(actor)})
                status = (await conn.execute(text(
                    "SELECT public.platform_saas_release_status() AS state"
                ).columns(state=JSONB))).scalar_one()
                assert status["operator_exemption_pending"] == 0
                assert status["billing_enabled"] is False
                assert status["registered_bots"] >= 0

                activated = (await conn.execute(text(
                    "SELECT public.platform_saas_release_change("
                    "'arm',:bots,:other_owners) AS state"
                ).columns(state=JSONB), {
                    "bots": status["registered_bots"],
                    "other_owners": status["nonoperator_owners"],
                })).scalar_one()
                assert activated["billing_enabled"] is True
                assert activated["billing_from"] is not None
            finally:
                await tx.rollback()
    finally:
        await app_engine.dispose()
        await admin.dispose()
