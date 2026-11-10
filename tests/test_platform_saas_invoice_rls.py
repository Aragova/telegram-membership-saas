"""Database tests for dormant, operator-protected ViMi Pay SaaS billing.

All privileged ledger writes run inside a rollback-only transaction. They
cannot enable the SaaS billing product or touch real tenant transactions.
"""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL with non-bypass runtime role",
)


def _tid():
    return uuid4().int % 7_000_000_000_000_000_000 + 1


def _chat():
    return "-100" + str(uuid4().int % 10**12).zfill(12)


async def _project(factory, telegram_id):
    user = await ensure_telegram_actor(
        factory, telegram_id=telegram_id, username=None, first_name=None,
    )
    project = await create_owner_project(
        factory, actor_id=user, name=f"Billing test {uuid4()}",
    )
    async with tenant_session_scope(
        factory, TenantContext(user_id=user, project_id=project),
    ) as session:
        merchant = await session.scalar(
            select(Project.merchant_id).where(Project.id == project),
        )
        bot = BotInstance(
            id=uuid4(), project_id=project, telegram_bot_id=_tid(),
            username="billing_test_bot", display_name="Billing test",
            token_ciphertext="test-only-placeholder", status="registered",
        )
        chat = _chat()
        resource = Resource(
            project_id=project, resource_type="telegram_channel",
            external_id=chat, name="Test channel",
        )
        session.add_all((bot, resource))
        await session.flush()
        return user, merchant, project, bot.id, chat


async def _invoice(admin, owner, merchant, bot, *, kind="prepayment", total="150.00"):
    invoice_id = uuid4()
    now = datetime.now(UTC)
    await admin.execute(text("""
        INSERT INTO platform_saas_invoices
        (id,invoice_key,merchant_id,owner_user_id,kind,bot_instance_id,
         period_start,period_end,due_at,total_rub)
        VALUES (:id,:key,:merchant,:owner,:kind,:bot,
                :start,:end,:due,:total)
    """), {
        "id": invoice_id, "key": str(uuid4()),
        "merchant": merchant, "owner": owner, "kind": kind,
        "bot": bot if kind == "prepayment" else None,
        "start": now, "end": now + timedelta(days=30),
        "due": now + timedelta(days=7), "total": total,
    })
    return invoice_id


async def _line(admin, invoice, project, source_kind, source_id, *,
                amount="150", currency="RUB", rate="1",
                fee_rate="1", charge="150.00"):
    await admin.execute(text("""
        INSERT INTO platform_saas_invoice_lines
        (id,invoice_id,project_id,source_kind,source_id,original_amount,
         original_currency,rub_per_unit,fee_rate,charge_rub)
        VALUES (:id,:invoice,:project,:kind,:source,:amount,:currency,
                :rate,:fee_rate,:charge)
    """), {
        "id": uuid4(), "invoice": invoice, "project": project,
        "kind": source_kind, "source": source_id, "amount": amount,
        "currency": currency, "rate": rate, "fee_rate": fee_rate,
        "charge": charge,
    })


async def _receipt(admin, invoice_id, actor, amount="150.00"):
    await admin.execute(text("""
        INSERT INTO platform_saas_receipts
        (id,invoice_id,confirmed_by,amount_rub)
        VALUES (:id,:invoice,:actor,:amount)
    """), {
        "id": uuid4(), "invoice": invoice_id, "actor": actor, "amount": amount,
    })


async def test_own_verified_channel_is_exempt_but_unverified_is_billable():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner_tg, stranger_tg = _tid(), _tid()
    try:
        owner, own_merchant, own_project, own_bot, chat = await _project(
            factory, owner_tg,
        )
        stranger, other_merchant, other_project, other_bot, other_chat = (
            await _project(factory, stranger_tg)
        )
        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                await admin.execute(text("""
                    INSERT INTO platform_operators (telegram_user_id,role)
                    VALUES (:id,'owner')
                """), {"id": owner_tg})
                await admin.execute(text("""
                    INSERT INTO platform_saas_owner_exemptions
                    (id,merchant_id,project_id,bot_instance_id,channel_chat_id,
                     owner_user_id,verified_by,proof_kind)
                    VALUES (:id,:merchant,:project,:bot,:chat,:owner,:owner,
                            'telegram_creator_verified')
                """), {
                    "id": uuid4(), "merchant": own_merchant,
                    "project": own_project, "bot": own_bot,
                    "chat": chat, "owner": owner,
                })

                own_invoice = await _invoice(admin, owner, own_merchant, own_bot)
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await _line(
                            admin, own_invoice, own_project,
                            "bot_fee", own_bot,
                        )
                assert getattr(error.value.orig, "sqlstate", None) == "42501"

                # A channel owned by another project owner does not inherit
                # the platform owner's exemption, even if he is an admin.
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text("""
                            INSERT INTO platform_saas_owner_exemptions
                            (id,merchant_id,project_id,bot_instance_id,
                             channel_chat_id,owner_user_id,verified_by,proof_kind)
                            VALUES (:id,:merchant,:project,:bot,:chat,:owner,
                                    :verifier,'telegram_creator_verified')
                        """), {
                            "id": uuid4(), "merchant": other_merchant,
                            "project": other_project, "bot": other_bot,
                            "chat": other_chat, "owner": stranger,
                            "verifier": owner,
                        })
                assert getattr(error.value.orig, "sqlstate", None) == "42501"

                external = await _invoice(
                    admin, stranger, other_merchant, other_bot,
                )
                await _line(
                    admin, external, other_project, "bot_fee", other_bot,
                )
                await admin.execute(text("""
                    UPDATE platform_saas_invoices SET status='issued'
                    WHERE id=:id
                """), {"id": external})
                stored = (await admin.execute(text("""
                    SELECT status,total_rub FROM platform_saas_invoices
                    WHERE id=:id
                """), {"id": external})).one()
                assert stored.status == "issued"
                assert stored.total_rub == Decimal("150.00")
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_receipt_requires_real_owner_confirmation_and_is_immutable():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner_tg, merchant_tg = _tid(), _tid()
    try:
        platform_owner = await ensure_telegram_actor(
            factory, telegram_id=owner_tg, username=None, first_name=None,
        )
        merchant, merchant_id, project, bot, _ = await _project(factory, merchant_tg)
        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                await admin.execute(text("""
                    INSERT INTO platform_operators(telegram_user_id,role)
                    VALUES (:id,'owner')
                """), {"id": owner_tg})
                invoice = await _invoice(
                    admin, merchant, merchant_id, bot,
                )
                await _line(admin, invoice, project, "bot_fee", bot)
                await admin.execute(text("""
                    UPDATE platform_saas_invoices SET status='issued'
                    WHERE id=:id
                """), {"id": invoice})
                await admin.execute(text("""
                    SELECT set_config('app.current_user_id',:id,true)
                """), {"id": str(merchant)})
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await _receipt(admin, invoice, merchant)
                assert getattr(error.value.orig, "sqlstate", None) == "42501"
                await admin.execute(text("""
                    SELECT set_config('app.current_user_id',:id,true)
                """), {"id": str(platform_owner)})
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await _receipt(admin, invoice, platform_owner, "149.00")
                assert getattr(error.value.orig, "sqlstate", None) == "23514"
                await _receipt(admin, invoice, platform_owner)

                with pytest.raises(DBAPIError):
                    async with admin.begin_nested():
                        await _receipt(admin, invoice, platform_owner)

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text("""
                            UPDATE platform_saas_invoices SET status='void'
                            WHERE id=:id
                        """), {"id": invoice})
                assert getattr(error.value.orig, "sqlstate", None) == "42501"

                with pytest.raises(DBAPIError):
                    async with admin.begin_nested():
                        await admin.execute(text("""
                            DELETE FROM platform_saas_receipts WHERE invoice_id=:id
                        """), {"id": invoice})
            finally:
                await tx.rollback()
        async with tenant_session_scope(
            factory, TenantContext(user_id=merchant, project_id=project),
        ) as session:
            with pytest.raises(DBAPIError):
                await session.execute(text("SELECT * FROM platform_saas_receipts"))
    finally:
        await engine.dispose()
        await admin_engine.dispose()


async def test_no_fake_approved_sale_duplicate_service_or_invoice_mutation():
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    try:
        merchant, merchant_id, project, bot, _ = await _project(factory, _tid())
        async with admin_engine.connect() as admin:
            tx = await admin.begin()
            try:
                invoice = await _invoice(admin, merchant, merchant_id, bot)
                await _line(admin, invoice, project, "bot_fee", bot)

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await _line(admin, invoice, project, "bot_fee", bot)
                assert getattr(error.value.orig, "sqlstate", None) == "23505"

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text("""
                            UPDATE platform_saas_invoices SET total_rub=75
                            WHERE id=:id
                        """), {"id": invoice})
                assert getattr(error.value.orig, "sqlstate", None) == "42501"

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        # Another invoice for this same bot/service period
                        # is not allowed, regardless of invoice_key.
                        now = (await admin.execute(text("""
                            SELECT period_start FROM platform_saas_invoices
                            WHERE id=:id
                        """), {"id": invoice})).scalar_one()
                        await admin.execute(text("""
                            INSERT INTO platform_saas_invoices
                            (id,invoice_key,merchant_id,owner_user_id,kind,
                             bot_instance_id,period_start,period_end,due_at,total_rub)
                            VALUES (:id,:key,:m,:o,'prepayment',:b,
                                    :start,:end,:due,150)
                        """), {
                            "id": uuid4(), "key": str(uuid4()), "m": merchant_id,
                            "o": merchant, "b": bot, "start": now,
                            "end": now + timedelta(days=30),
                            "due": now + timedelta(days=7),
                        })
                assert getattr(error.value.orig, "sqlstate", None) == "23505"

                invalid = await _invoice(
                    admin, merchant, merchant_id, None,
                    kind="commission", total="25",
                )
                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await _line(
                            admin, invalid, project, "subscription", uuid4(),
                            amount="500", fee_rate="0.05", charge="25",
                        )
                assert getattr(error.value.orig, "sqlstate", None) == "23514"

                with pytest.raises(DBAPIError) as error:
                    async with admin.begin_nested():
                        await admin.execute(text("""
                            UPDATE platform_saas_invoices SET status='issued'
                            WHERE id=:id
                        """), {"id": invalid})
                assert getattr(error.value.orig, "sqlstate", None) == "23514"
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin_engine.dispose()
