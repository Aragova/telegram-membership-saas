"""Rollback-only PostgreSQL E2E: frozen settlement and audited credits.

No production billing is enabled and every financial test insert rolls back.
"""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and privileged rollback-only transaction",
)


async def _actor(conn, actor):
    await conn.execute(
        text("SELECT set_config('app.current_user_id',:actor,true)"),
        {"actor": str(actor)},
    )


async def _lock(conn, invoice, currency):
    return (await conn.execute(text(
        "SELECT public.platform_saas_select_invoice_currency("
        ":invoice,:currency) AS quote"
    ).columns(quote=JSONB), {
        "invoice": invoice, "currency": currency,
    })).scalar_one()


async def _confirm(conn, invoice, amount, currency="USD", reference="bank:001"):
    return (await conn.execute(text(
        "SELECT public.platform_saas_confirm_currency_receipt("
        ":invoice,:currency,:amount,:reference)"
    ), {
        "invoice": invoice, "currency": currency,
        "amount": amount, "reference": reference,
    })).scalar_one()


async def test_settlement_snapshot_uses_quote_day_and_rejects_resettlement():
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(cfg.migration_database_url)
    try:
        operator_tg, merchant_tg, outsider_tg = _tid(), _tid(), _tid()
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=outsider_tg, username=None, first_name=None,
        )
        owner, merchant, project, bot, _ = await _project(factory, merchant_tg)
        today = datetime.now(UTC).astimezone(ZoneInfo("Europe/Moscow")).date()
        async with admin_engine.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": operator_tg})
                invoice = await _invoice(conn, owner, merchant, bot)
                await _line(conn, invoice, project, "bot_fee", bot)
                await conn.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued'"
                    " WHERE id=:id"
                ), {"id": invoice})

                await _actor(conn, owner)
                preview_sql = text(
                    "SELECT public.platform_saas_preview_invoice_currency("
                    ":id,'USD') AS data"
                ).columns(data=JSONB)
                preview_missing = (await conn.execute(
                    preview_sql, {"id": invoice},
                )).scalar_one()
                assert preview_missing["preview_only"] is True
                assert preview_missing["quote_ready"] is False
                assert preview_missing["amount"] is None
                with pytest.raises(DBAPIError) as no_rate:
                    async with conn.begin_nested():
                        await _lock(conn, invoice, "USD")
                assert getattr(no_rate.value.orig, "sqlstate", None) == "22023"

                await _actor(conn, operator)
                await conn.execute(text(
                    "SELECT public.platform_saas_publish_cbr_daily_fx("
                    ":day,'USD',:rate,:day)"
                ), {"day": today, "rate": Decimal("75")})
                await _actor(conn, outsider)
                with pytest.raises(DBAPIError) as outsider_error:
                    async with conn.begin_nested():
                        await _lock(conn, invoice, "USD")
                assert getattr(
                    outsider_error.value.orig, "sqlstate", None
                ) == "42501"

                await _actor(conn, owner)
                preview_ready = (await conn.execute(
                    preview_sql, {"id": invoice},
                )).scalar_one()
                assert preview_ready["quote_ready"] is True
                assert Decimal(str(preview_ready["amount"])) == Decimal("2")
                assert Decimal(str(preview_ready["rub_per_unit"])) == Decimal("75")
                assert preview_ready["quote_day"] == today.isoformat()
                assert (await conn.execute(text(
                    "SELECT count(*) FROM platform_saas_currency_settlements"
                    " WHERE invoice_id=:id"
                ), {"id": invoice})).scalar_one() == 0
                # CI creates the non-bypass membership_runtime role AFTER
                # Alembic: check its actual EXECUTE grants, not admin-only.
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                guarded = text(
                    "SELECT public.platform_saas_lock_invoice_currency_expected("
                    ":invoice,'USD',:day,:rate,:amount) AS data"
                ).columns(data=JSONB)
                quote_args = {
                    "invoice": invoice, "day": today, "rate": Decimal("75"),
                    "amount": Decimal("2.00"),
                }
                for changed in (
                    {"day": today - timedelta(days=1)},
                    {"rate": Decimal("76")},
                    {"amount": Decimal("1.99")},
                ):
                    with pytest.raises(DBAPIError) as stale:
                        async with conn.begin_nested():
                            await conn.execute(
                                guarded, {**quote_args, **changed},
                            )
                    assert getattr(stale.value.orig, "sqlstate", None) == "22023"
                    unlocked = (await conn.execute(text(
                        "SELECT public.platform_saas_invoice_currency_snapshot("
                        ":invoice) AS data"
                    ).columns(data=JSONB), {"invoice": invoice})).scalar_one()
                    assert unlocked["locked"] is False
                quote = (await conn.execute(guarded, quote_args)).scalar_one()
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT * FROM platform_saas_currency_settlements"
                        ))
                await conn.execute(text("RESET ROLE"))
                assert quote["locked"] is True
                count_foreign = text(
                    "SELECT public.platform_saas_owner_foreign_invoice_count()"
                )
                assert (await conn.execute(count_foreign)).scalar_one() == 1
                with pytest.raises(DBAPIError) as quote_already_locked:
                    async with conn.begin_nested():
                        await conn.execute(preview_sql, {"id": invoice})
                assert getattr(
                    quote_already_locked.value.orig, "sqlstate", None
                ) == "42501"
                history = (await conn.execute(text(
                    "SELECT public.platform_saas_invoice_credit_history(:id) AS data"
                ).columns(data=JSONB), {"id": invoice})).scalar_one()
                assert history["credit_count"] == 0
                assert history["entries"] == []
                assert quote["currency"] == "USD"
                assert quote["quote_day"] == today.isoformat()
                assert Decimal(str(quote["rub_per_unit"])) == Decimal("75")
                assert Decimal(str(quote["total_rub"])) == Decimal("150")
                assert Decimal(str(quote["amount"])) == Decimal("2")
                assert await _lock(conn, invoice, "USD") == quote
                with pytest.raises(DBAPIError) as immutable:
                    async with conn.begin_nested():
                        await _lock(conn, invoice, "EUR")
                assert getattr(immutable.value.orig, "sqlstate", None) == "23505"

                snapshot = (await conn.execute(text(
                    "SELECT public.platform_saas_invoice_currency_snapshot("
                    ":invoice) AS quote"
                ).columns(quote=JSONB), {"invoice": invoice})).scalar_one()
                assert snapshot["currency"] == "USD"
                assert snapshot["paid"] is False
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "UPDATE platform_saas_currency_settlements"
                            " SET rub_per_unit=50 WHERE invoice_id=:id"
                        ), {"id": invoice})

                await _actor(conn, operator)
                # Old RUB-only receipt action may not silently clear a
                # USD-locked invoice, even when called by platform owner.
                with pytest.raises(DBAPIError) as old_path:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_confirm_receipt("
                            ":invoice,NULL)"
                        ), {"invoice": invoice})
                assert getattr(old_path.value.orig, "sqlstate", None) == "42501"
                with pytest.raises(DBAPIError) as mismatch:
                    async with conn.begin_nested():
                        await _confirm(conn, invoice, Decimal("1.99"))
                assert getattr(mismatch.value.orig, "sqlstate", None) == "23514"

                await _actor(conn, owner)
                with pytest.raises(DBAPIError) as unauthorized:
                    async with conn.begin_nested():
                        await _confirm(conn, invoice, Decimal("2"))
                assert getattr(
                    unauthorized.value.orig, "sqlstate", None
                ) == "42501"
                await _actor(conn, operator)
                receipt = await _confirm(conn, invoice, Decimal("2"))
                assert await _confirm(conn, invoice, Decimal("2")) == receipt
                await _actor(conn, owner)
                assert (await conn.execute(count_foreign)).scalar_one() == 0
                await _actor(conn, operator)
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await _confirm(
                            conn, invoice, Decimal("2"), reference="bank:002",
                        )
                paid = (await conn.execute(text(
                    "SELECT r.amount_rub,c.received_amount,c.received_currency "
                    "FROM platform_saas_receipts r "
                    "JOIN platform_saas_currency_receipts c "
                    "ON c.receipt_id=r.id WHERE r.id=:id"
                ), {"id": receipt})).one()
                assert Decimal(paid.amount_rub) == Decimal("150")
                assert Decimal(paid.received_amount) == Decimal("2")
                assert paid.received_currency == "USD"
                with pytest.raises(DBAPIError):
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "DELETE FROM platform_saas_currency_receipts"
                            " WHERE invoice_id=:id"
                        ), {"id": invoice})

                # A bot subscription prepayment is not a reimbursable
                # sales commission; credit RPC cannot invent a refund.
                line = (await conn.execute(text(
                    "SELECT id FROM platform_saas_invoice_lines "
                    "WHERE invoice_id=:id"
                ), {"id": invoice})).scalar_one()
                with pytest.raises(DBAPIError) as not_sale:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_record_refund_credit("
                            ":line,'refund:00001',10,'refund')"
                        ), {"line": line})
                assert getattr(not_sale.value.orig, "sqlstate", None) == "42501"
                assert (await conn.execute(text(
                    "SELECT enabled FROM platform_saas_billing_control "
                    "WHERE id=1"
                ))).scalar_one() is False
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()
