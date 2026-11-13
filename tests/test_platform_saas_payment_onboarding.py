"""PostgreSQL integration: secure platform bank details and merchant notices.

Financial changes are inside one privileged transaction explicitly rolled back.
No real billing, trial, hold flag or merchant account is activated.
"""
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _invoice, _line, _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.platform_saas_notices import _notice_text

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires PostgreSQL migration and non-bypass runtime role",
)


async def _bind(admin, actor):
    await admin.execute(text(
        "SELECT set_config('app.current_user_id',:value,true)"
    ), {"value": str(actor)})


async def _json(admin, statement, args=None):
    return (await admin.execute(text(
        statement + " AS data"
    ).columns(data=JSONB), args or {})).scalar_one()


async def test_owner_billing_instructions_and_cross_tenant_invoice_privacy():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    platform_tg = _tid()
    try:
        operator = await ensure_telegram_actor(
            factory, telegram_id=platform_tg, username=None, first_name=None,
        )
        customer_a, merchant_a, project_a, bot_a, _ = await _project(factory, _tid())
        customer_b, _, _, _, _ = await _project(factory, _tid())
        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                await admin.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES (:tid,'owner')"
                ), {"tid": platform_tg})
                await _bind(admin, operator)
                before = await _json(
                    admin, "SELECT public.platform_saas_payment_details_read()"
                )
                assert before["configured"] is False
                assert before["billing_enabled"] is False
                assert before["trial_enabled"] is False
                assert before["holds_enabled"] is False
                sample_bank = "TEST BANK · 1234567890 & <transfer> recipient"
                ok = (await admin.execute(text(
                    "SELECT public.platform_saas_payment_details_set(:bank)"
                ), {"bank": sample_bank})).scalar_one()
                assert ok is True
                configured = await _json(
                    admin, "SELECT public.platform_saas_payment_details_read()"
                )
                assert configured["instructions"] == sample_bank

                invoice = await _invoice(
                    admin, customer_a, merchant_a, bot_a,
                    period_start=datetime.now(UTC) - timedelta(days=2),
                )
                await _line(admin, invoice, project_a, "bot_fee", bot_a)
                await admin.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
                ), {"id": invoice})

                await _bind(admin, customer_a)
                own = await _json(
                    admin, "SELECT public.platform_saas_owner_invoice_report(0,6)"
                )
                assert own["total"] == 1
                assert own["unpaid"] == 1
                assert own["invoices"][0]["id"] == str(invoice)
                assert own["instructions"] == sample_bank

                await _bind(admin, customer_b)
                other = await _json(
                    admin, "SELECT public.platform_saas_owner_invoice_report(0,6)"
                )
                assert other["total"] == 0
                assert other["invoices"] == []
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "SELECT public.platform_saas_payment_details_set("
                            "'FORGED ACCOUNT 12345')"
                        ))
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text(
                            "SELECT public.platform_saas_payment_details_read()"
                        ))
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_invoice_notice_outbox_dedupes_retries_and_stops_after_receipt():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    platform_tg = _tid()
    try:
        platform_actor = await ensure_telegram_actor(
            factory, telegram_id=platform_tg, username=None, first_name=None,
        )
        liable, merchant, project, bot, _ = await _project(factory, _tid())
        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                await admin.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tid,'owner')"
                ), {"tid": platform_tg})
                await _bind(admin, platform_actor)
                invoice = await _invoice(
                    admin, liable, merchant, bot,
                    period_start=datetime.now(UTC) - timedelta(days=2),
                )
                await _line(admin, invoice, project, "bot_fee", bot)
                await admin.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
                ), {"id": invoice})
                # No bank details, billing switch OFF: never send.
                assert (await admin.execute(text(
                    "SELECT public.platform_saas_notice_schedule()"
                ))).scalar_one() == 0
                # The new catalog is the sole payment availability source.
                # No legacy v2/bank method is configured in this test.
                await admin.execute(text(
                    "SELECT public.platform_saas_catalog_create("
                    ":name,:instructions,:currency)"
                ), {
                    "name": "Manual RUB transfer", "currency": "RUB",
                    "instructions": "TEST PAYMENT DETAILS 1234567890 <tag> & data",
                })
                assert (await admin.execute(text(
                    "SELECT public.platform_saas_notice_schedule()"
                ))).scalar_one() == 0

                # Use this statement's actual issue timestamp as rollout.
                # Other integration tests may leave older issued invoices
                # in their shared disposable CI database.
                issued_at = (await admin.execute(text(
                    "SELECT issued_at FROM platform_saas_invoices WHERE id=:id"
                ), {"id": invoice})).scalar_one()
                await admin.execute(text("""
                    UPDATE platform_saas_billing_control
                      SET enabled=true,effective_from=:started WHERE id=1
                """), {"started": issued_at})
                assert (await admin.execute(text(
                    "SELECT public.platform_saas_notice_schedule()"
                ))).scalar_one() == 1
                assert (await admin.execute(text(
                    "SELECT public.platform_saas_notice_schedule()"
                ))).scalar_one() == 0

                claimed = await _json(
                    admin, "SELECT public.platform_saas_notice_claim()"
                )
                assert claimed["kind"] == "issued"
                assert claimed["recipient"] > 0
                assert claimed["invoice_id"] == str(invoice)
                # Notices must never expose outdated payment details. Owners
                # select active bank credentials on the live invoice screen.
                assert "instructions" not in claimed
                assert "<tag>" not in _notice_text(claimed)
                assert "TEST PAYMENT DETAILS" not in _notice_text(claimed)
                assert "ViMi Pay" in _notice_text(claimed)

                delivered = (await admin.execute(text(
                    "SELECT public.platform_saas_notice_finish("
                    ":id,:attempt,true)"
                ), {
                    "id": UUID(claimed["id"]),
                    "attempt": claimed["attempt"],
                })).scalar_one()
                assert delivered is True
                again = (await admin.execute(text(
                    "SELECT public.platform_saas_notice_finish("
                    ":id,:attempt,true)"
                ), {
                    "id": UUID(claimed["id"]), "attempt": claimed["attempt"],
                })).scalar_one()
                assert again is False

                await admin.execute(text("""
                    INSERT INTO platform_saas_receipts
                    (id,invoice_id,confirmed_by,amount_rub)
                    VALUES (:id,:invoice,:actor,150.00)
                """), {
                    "id": uuid4(), "invoice": invoice,
                    "actor": platform_actor,
                })
                assert (await admin.execute(text(
                    "SELECT public.platform_saas_notice_schedule()"
                ))).scalar_one() == 0
                assert await _json(
                    admin, "SELECT public.platform_saas_notice_claim()"
                ) is None
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()



async def test_stale_issued_notice_is_cancelled_after_delivery_pause():
    """Old issued stage never replays with an overdue notice after a pause."""
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    admin_engine = create_async_engine(settings.migration_database_url)
    platform_tg = _tid()
    try:
        operator = await ensure_telegram_actor(
            factory, telegram_id=platform_tg, username=None, first_name=None,
        )
        owner, merchant, project, bot, _ = await _project(factory, _tid())
        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                await admin.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tid,'owner')"
                ), {"tid": platform_tg})
                await _bind(admin, operator)
                moment = datetime.now(UTC)
                invoice = await _invoice(
                    admin, owner, merchant, bot,
                    period_start=moment - timedelta(days=16),
                )
                await _line(admin, invoice, project, "bot_fee", bot)
                await admin.execute(text(
                    "UPDATE platform_saas_invoices SET status='issued' WHERE id=:id"
                ), {"id": invoice})
                # Simulate a real old outstanding invoice in the isolated
                # rollback-only CI transaction. Disable the immutable
                # financial guard exclusively for this fixture timestamp,
                # restoring it immediately before exercising the RPCs.
                await admin.execute(text(
                    "ALTER TABLE platform_saas_invoices DISABLE TRIGGER USER"
                ))
                await admin.execute(text(
                    "UPDATE platform_saas_invoices SET issued_at=:issued WHERE id=:id"
                ), {
                    "id": invoice,
                    "issued": moment - timedelta(days=11),
                })
                await admin.execute(text(
                    "ALTER TABLE platform_saas_invoices ENABLE TRIGGER USER"
                ))
                issued_notice = uuid4()
                await admin.execute(text("""
                    INSERT INTO platform_saas_notice_outbox
                      (id,invoice_id,kind,status)
                    VALUES (:id,:invoice,'issued','pending')
                """), {"id": issued_notice, "invoice": invoice})
                await admin.execute(text("""
                    UPDATE platform_saas_billing_control
                       SET enabled=true,effective_from=:from WHERE id=1
                """), {"from": moment - timedelta(days=30)})
                # No enabled catalog method during the delivery pause,
                # so the stale issued stage stays pending.
                assert (await admin.execute(text(
                    "SELECT public.platform_saas_notice_schedule()"
                ))).scalar_one() == 0
                await admin.execute(text(
                    "SELECT public.platform_saas_catalog_create("
                    ":name,:instructions,:currency)"
                ), {
                    "name": "New RUB method", "currency": "RUB",
                    "instructions": "TEST BANK ROUTING 1234567890",
                })
                await admin.execute(text(
                    "SELECT public.platform_saas_notice_schedule()"
                ))
                stages = (await admin.execute(text("""
                    SELECT kind,status FROM platform_saas_notice_outbox
                    WHERE invoice_id=:invoice
                """), {"invoice": invoice})).all()
                assert dict(stages)["issued"] == "cancelled"
                assert dict(stages)["overdue"] == "pending"

                # Recreate the previous stale pending state to test the
                # defensive check inside claim() independently of schedule.
                await admin.execute(text("""
                    UPDATE platform_saas_notice_outbox
                       SET status='pending' WHERE id=:id
                """), {"id": issued_notice})
                await admin.execute(text("""
                    UPDATE platform_saas_notice_outbox
                       SET available_at=clock_timestamp()+interval '30 days'
                    WHERE id<>:id AND status='pending'
                """), {"id": issued_notice})
                claimed = await _json(
                    admin, "SELECT public.platform_saas_notice_claim()"
                )
                assert claimed is None
                current = (await admin.execute(text(
                    "SELECT status FROM platform_saas_notice_outbox WHERE id=:id"
                ), {"id": issued_notice})).scalar_one()
                assert current == "cancelled"
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()
