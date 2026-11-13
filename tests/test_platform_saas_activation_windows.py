"""Rollback-only activation-history regression against the real database.

Cancelling the pending five-minute rollout must never create a phantom
billable interval; STOP/ARM must retain previously earned obligations.
"""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL with privileged rollback",
)


async def test_cancelled_rollout_does_not_fabricate_commercial_sales():
    admin = create_async_engine(Settings().migration_database_url)
    now = datetime.now(UTC)
    actor = uuid4()
    first_arm_at = now - timedelta(minutes=30)
    first_effective = now - timedelta(minutes=25)
    cancel_at = now - timedelta(minutes=29)
    second_arm_at = now - timedelta(minutes=20)
    second_effective = now - timedelta(minutes=15)
    pause_at = now - timedelta(minutes=10)
    third_arm_at = now - timedelta(minutes=5)
    third_effective = now
    try:
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                for action, created, effective in (
                    ("arm", first_arm_at, first_effective),
                    ("stop", cancel_at, None),
                    ("arm", second_arm_at, second_effective),
                    ("stop", pause_at, None),
                    ("arm", third_arm_at, third_effective),
                ):
                    await conn.execute(text(
                        "INSERT INTO public.platform_saas_release_audit"
                        " (id,actor_id,action,registered_bots,nonoperator_owners,"
                        "  effective_from,created_at)"
                        " VALUES(:id,:actor,:action,1,1,:effective,:created)"
                    ), {
                        "id": uuid4(), "actor": actor, "action": action,
                        "effective": effective, "created": created,
                    })
                first = (await conn.execute(text(
                    "SELECT public.platform_saas_first_activation()"
                ))).scalar_one()
                assert first == second_effective
                statement = text(
                    "SELECT public.platform_saas_sale_active_at(:sale)"
                )
                for at, expected in (
                    (first_effective + timedelta(minutes=1), False),
                    (second_effective + timedelta(minutes=1), True),
                    (pause_at + timedelta(minutes=1), False),
                    (third_effective + timedelta(minutes=1), True),
                ):
                    active = (await conn.execute(
                        statement, {"sale": at},
                    )).scalar_one()
                    assert active is expected
            finally:
                await tx.rollback()
    finally:
        await admin.dispose()
