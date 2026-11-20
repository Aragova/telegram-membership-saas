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
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
)
from telegram_membership_saas.modules.platform_saas_v2_payment_service import (
    saas_price_profile,
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
        runtime = build_engine(settings)
        factory = build_session_factory(runtime)
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
        # Exercise the actual Python/RPC path as well as SQL so a reader
        # missing from the closed allowlist cannot silently bypass the wizard.
        pending = await saas_price_profile(
            factory, actor_id=owner, project_id=project2,
        )
        assert pending["currency_selected"] is False
        assert pending["currency"] == "RUB"
        async with admin.connect() as conn:
            outer = await conn.begin()
            try:
                # Canonical platform billing must gate invoices at SQL-level,
                # not just hide the dashboard until the owner chooses currency.
                issuer_sql = (await conn.execute(text(
                    "SELECT pg_get_functiondef(" 
                    "'public.platform_saas_issue_bot_invoice("
                    "uuid,timestamptz,timestamptz)'::regprocedure)"
                ))).scalar_one()
                assert "currency_selected" in issuer_sql
                assert issuer_sql.index("pg_advisory_xact_lock") < (
                    issuer_sql.index("currency_selected")
                )
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(owner)})
                # Newly registered bots inherit a safe provisional RUB
                # profile, but do not count it as the owner's choice.
                initial1 = await rpc(
                    conn, "platform_saas_bot_price_profile_read_v2", project1,
                )
                initial2 = await rpc(
                    conn, "platform_saas_bot_price_profile_read_v2", project2,
                )
                assert initial1["currency_selected"] is False
                assert initial2["currency_selected"] is False
                a = await rpc(
                    conn, "platform_saas_bot_price_profile_set", project1, "USD",
                )
                b = await rpc(
                    conn, "platform_saas_bot_price_profile_read_v2", project2,
                )
                assert b["currency_selected"] is False
                # Explicitly accepting the suggested same RUB value
                # must still finish the final onboarding stage.
                await rpc(
                    conn, "platform_saas_bot_price_profile_set", project2, "RUB",
                )
                selected = await rpc(
                    conn, "platform_saas_bot_price_profile_read_v2", project2,
                )
                assert selected["currency_selected"] is True
                selected1 = await rpc(
                    conn, "platform_saas_bot_price_profile_read_v2", project1,
                )
                assert selected1["currency_selected"] is True
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
        await runtime.dispose()
