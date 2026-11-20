"""Independent monthly currency choice per ViMi Pay bot.

Real PostgreSQL RPCs with rollback-only financial fixtures, never public bots.
An unpaid bill freezes only the originating bot's next service currency.
"""
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and non-bypass membership_runtime role",
)


async def rpc(conn, name, *params):
    stmt = text("SELECT public." + name + "("
                + ",".join(":x" + str(i) for i in range(len(params)))
                + ") AS data").columns(data=JSONB)
    return (await conn.execute(
        stmt, {"x" + str(i): p for i, p in enumerate(params)}
    )).scalar_one()


async def test_two_owned_bots_have_independent_price_and_unpaid_lock():
    settings = Settings()
    admin = create_async_engine(settings.migration_database_url)
    try:
        factory = async_sessionmaker(
            create_async_engine(settings.database_url), expire_on_commit=False,
        )
        owner, merchant, project1, bot1, _ = await _project(factory, _tid())
        # An independent second owned project has a separate registered bot.
        project2 = await create_owner_project(
            factory, actor_id=owner, name="Other bot currency " + str(uuid4()),
        )
        from telegram_membership_saas.db import tenant_session_scope
        from telegram_membership_saas.tenant_context import TenantContext
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project2),
        ) as session:
            bot2 = uuid4()
            session.add(BotInstance(
                id=bot2, project_id=project2, telegram_bot_id=_tid(),
                username="other_currency_bot", display_name="Other bot",
                token_ciphertext="fixture-not-a-real-token", status="registered",
            ))
            await session.flush()
        async with admin.connect() as conn:
            outer = await conn.begin()
            try:
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(owner)})
                a = await rpc(
                    conn, "platform_saas_bot_price_profile_set", project1, "USD",
                )
                b = await rpc(
                    conn, "platform_saas_bot_price_profile_read", project2,
                )
                assert a["currency"] == "USD" and a["monthly_amount"] == "5.00"
                assert b["currency"] == "RUB" and b["monthly_amount"] == "150.00"
                await conn.execute(text("RESET ROLE"))
                # A real issued RUB debt belongs ONLY to bot1. Do not mutate
                # its issued amount after changing prospective preferences.
                inv = await _invoice(
                    conn, owner, merchant, bot1, total="150.00",
                )
                await _line(conn, inv, project1, "bot_fee", bot1)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
                ), {"id": inv})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(owner)})
                with pytest.raises(DBAPIError) as blocked:
                    async with conn.begin_nested():
                        await rpc(
                            conn, "platform_saas_bot_price_profile_set",
                            project1, "RUB",
                        )
                assert getattr(blocked.value.orig, "sqlstate", None) == "23514"
                # The other bot's currency remains independently editable.
                changed = await rpc(
                    conn, "platform_saas_bot_price_profile_set", project2, "USD",
                )
                assert changed["currency"] == "USD"
                assert (await rpc(
                    conn, "platform_saas_bot_price_profile_read", project1,
                ))["currency"] == "USD"
                await conn.execute(text("RESET ROLE"))
                due = (await conn.execute(text(
                    "SELECT total_rub FROM platform_saas_invoices WHERE id=:id"
                ), {"id": inv})).scalar_one()
                assert str(due) in ("150.00", "150.000000000000000000")
            finally:
                await outer.rollback()
    finally:
        await admin.dispose()
        await factory.kw["bind"].dispose()
