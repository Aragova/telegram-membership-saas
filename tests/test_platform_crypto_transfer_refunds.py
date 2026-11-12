"""Migrated PostgreSQL: original-app Crypto Pay transfer refund credit ledger.

Every privileged database mutation is rolled back; this test NEVER sends money
or activates commercial billing, checkout or subscriber access.
"""
import os
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_crypto_pay_atomic_fulfillment import _shop
from test_crypto_pay_invoice_reservations import _tid
from test_platform_crypto_commission_evidence import CryptoAssetProvider

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
)
from telegram_membership_saas.modules.crypto_pay.checkout import (
    issue_reserved_crypto_invoice,
    reserve_crypto_invoice,
)
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayInvoice
from telegram_membership_saas.modules.crypto_pay.settlement import (
    settle_crypto_pay_invoice,
)
from telegram_membership_saas.modules.file_sales.manual_models import FileManualPrice
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Real migrated PostgreSQL and privileged rollback are required",
)


async def test_original_transfer_refunds_are_immutable_capped_and_not_billed():
    CryptoAssetProvider.created = []
    CryptoAssetProvider.invoice_data = {}
    CryptoAssetProvider.paid_rate = "3.25"
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        owner, project_id, bot_id, product_id = await _shop(factory, key)
        async with system_project_session_scope(factory, project_id) as session:
            session.add(FileManualPrice(
                project_id=project_id, product_id=product_id,
                currency_code="TON", amount=Decimal("1.5"),
            ))
        buyer = _tid()
        invoice_id = await reserve_crypto_invoice(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer, kind="file", target_id=product_id, currency="TON",
        )
        await issue_reserved_crypto_invoice(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=buyer, invoice_id=invoice_id,
            encryption_key=key, client_factory=CryptoAssetProvider,
        )
        settled = await settle_crypto_pay_invoice(
            factory, project_id=project_id, invoice_id=invoice_id,
            encryption_key=key, client_factory=CryptoAssetProvider,
        )
        assert settled.changed
        operator_tg = _tid()
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "UPDATE crypto_pay_apps SET environment='mainnet' "
                    "WHERE project_id=:p"
                ), {"p": project_id})
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": operator_tg})
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:id,true)"
                ), {"id": str(operator)})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                capture = (await conn.execute(text(
                    "SELECT public.platform_saas_capture_crypto_sales(200)"
                    " AS data"
                ).columns(data=JSONB))).scalar_one()
                assert capture["recorded"] >= 1
                await conn.execute(text("RESET ROLE"))
                sale = (await conn.execute(text(
                    "SELECT id,commission_usd,gross_usd,billing_eligible "
                    "FROM platform_saas_crypto_sales WHERE invoice_id=:id"
                ), {"id": invoice_id})).one()
                assert sale.commission_usd > 0
                assert not sale.billing_eligible
                first_spend = f"vimi-refund:{invoice_id}:aabbcc11"
                second_spend = f"vimi-refund:{invoice_id}:aabbcc22"

                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                candidate = (await conn.execute(text(
                    "SELECT public.platform_saas_crypto_refund_candidate("
                    ":sale,:spend) AS data"
                ).columns(data=JSONB), {
                    "sale": sale.id, "spend": first_spend,
                })).scalar_one()
                assert candidate["buyer_id"] == buyer
                assert candidate["app_id"]
                assert candidate["asset"] == "TON"

                # The merchant or a buyer cannot attribute transfers as
                # platform commission credits even if they know the IDs.
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:id,true)"
                ), {"id": str(owner)})
                with pytest.raises(DBAPIError) as forbidden:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_crypto_refund_candidate("
                            ":sale,:spend)"
                        ), {"sale": sale.id, "spend": first_spend})
                assert getattr(forbidden.value.orig, "sqlstate", None) == "42501"
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:id,true)"
                ), {"id": str(operator)})

                statement = text(
                    "SELECT public.platform_saas_record_crypto_transfer_credit("
                    ":sale,:transfer,:spend,:buyer,:asset,:amount,:at)"
                )
                at = datetime.now(UTC)
                first = {
                    "sale": sale.id, "transfer": 40001,
                    "spend": first_spend, "buyer": buyer,
                    "asset": "TON", "amount": Decimal("0.5"),
                    "at": at,
                }
                record_id = (await conn.execute(statement, first)).scalar_one()
                assert (await conn.execute(statement, first)).scalar_one() == record_id

                for bad in (
                    {**first, "buyer": buyer + 1, "transfer": 40002,
                     "spend": second_spend},
                    {**first, "asset": "BTC", "transfer": 40002,
                     "spend": second_spend},
                    {**first, "amount": Decimal("0.5001")},
                    {**first, "transfer": 40002,
                     "spend": first_spend},
                ):
                    with pytest.raises(DBAPIError):
                        async with conn.begin_nested():
                            await conn.execute(statement, bad)

                second = {
                    **first, "transfer": 40002, "spend": second_spend,
                    "amount": Decimal("1.0"),
                }
                await conn.execute(statement, second)
                with pytest.raises(DBAPIError) as over_cap:
                    async with conn.begin_nested():
                        await conn.execute(statement, {
                            **second, "transfer": 40003,
                            "spend": f"vimi-refund:{invoice_id}:aabbcc33",
                            "amount": Decimal("0.01"),
                        })
                assert getattr(over_cap.value.orig, "sqlstate", None) == "23514"

                # Neither platform finance nor a connected buyer can inspect
                # the global immutable credit ledger directly.
                with pytest.raises(DBAPIError) as direct:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT * FROM platform_saas_crypto_transfer_credits"
                        ))
                assert getattr(direct.value.orig, "sqlstate", None) == "42501"

                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:id,true)"
                ), {"id": str(owner)})
                month = datetime.now(UTC).astimezone(
                    ZoneInfo("Europe/Moscow")
                ).date().replace(day=1)
                refunds = (await conn.execute(text(
                    "SELECT public.platform_saas_crypto_refund_summary("
                    ":p,:m) AS data"
                ).columns(data=JSONB), {
                    "p": project_id, "m": month,
                })).scalar_one()
                assert refunds["refunds"] == 2
                assert Decimal(refunds["gross_credit_usd"]) == sale.gross_usd
                assert Decimal(refunds["commission_credit_usd"]) == sale.commission_usd
                assert Decimal(refunds["eligible_commission_credit_usd"]) == 0
                assert refunds["applied_to_invoices"] is False
                await conn.execute(text("RESET ROLE"))
                count = (await conn.execute(text(
                    "SELECT count(*) FROM platform_saas_crypto_transfer_credits "
                    "WHERE original_sale_id=:s"
                ), {"s": sale.id})).scalar_one()
                assert count == 2
                with pytest.raises(DBAPIError) as immutable:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "UPDATE platform_saas_crypto_transfer_credits "
                            "SET asset_amount=0.1 WHERE original_sale_id=:s"
                        ), {"s": sale.id})
                assert getattr(immutable.value.orig, "sqlstate", None) == "42501"
            finally:
                await tx.rollback()
        async with system_project_session_scope(factory, project_id) as session:
            original = await session.get(CryptoPayInvoice, invoice_id)
            assert original.status == "paid"
    finally:
        await admin.dispose()
        await engine.dispose()
