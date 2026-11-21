"""Rollback-only PostgreSQL contract for the six-rail native sales preview.

No test sales, invoice or FX rows persist after the privileged transaction.
The report must reject foreign owners through the non-bypass runtime role.
"""
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated PostgreSQL and non-bypass runtime RLS required",
)


async def test_six_rails_preserve_native_amounts_and_reject_foreign_owner():
    config = Settings()
    engine = build_engine(config)
    factory = build_session_factory(engine)
    admin = create_async_engine(config.migration_database_url)
    owner, merchant, project, bot_id, _ = await _project(factory, _tid())
    outsider, _, other_project, _, _ = await _project(factory, _tid())
    now = datetime.now(UTC)
    month = now.astimezone(ZoneInfo("Europe/Moscow")).date().replace(day=1)
    try:
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                telegram_bot_id = (await conn.execute(text(
                    "SELECT telegram_bot_id FROM bot_instances WHERE id=:bot"
                ), {"bot": bot_id})).scalar_one()
                folder, product, manual_sale = uuid4(), uuid4(), uuid4()
                await conn.execute(text(
                    "INSERT INTO file_sale_folders(id,project_id,name,status) "
                    "VALUES(:id,:project,'Preview folder','active')"
                ), {"id": folder, "project": project})
                await conn.execute(text(
                    "INSERT INTO file_sale_products("
                    "id,project_id,folder_id,telegram_bot_id,name,stars_price,status) "
                    "VALUES(:id,:project,:folder,:bot,'Preview file',200,'published')"
                ), {"id": product, "project": project,
                    "folder": folder, "bot": telegram_bot_id})
                await conn.execute(text(
                    "INSERT INTO file_sale_manual_orders("
                    "id,project_id,product_id,telegram_bot_id,"
                    "telegram_user_id,product_name,amount,currency_code,"
                    "payment_method_id,payment_method_name,instruction,status,paid_at) "
                    "VALUES(:id,:project,:product,:bot,987654,'Test file',"
                    "120,'RUB',:method,'Manual','Test','approved',:at)"
                ), {"id": manual_sale, "project": project,
                    "product": product, "bot": telegram_bot_id,
                    "method": uuid4(), "at": now})

                # Telegram Stars file purchase confirmed without a reward
                # capture: its native amount must still be visible, but not
                # counted again if the capture subsequently materializes.
                await conn.execute(text(
                    "INSERT INTO file_sale_purchases("
                    "id,project_id,product_id,telegram_bot_id,"
                    "telegram_user_id,stars_amount,product_name,"
                    "invoice_payload,telegram_charge_id,status,paid_at)"
                    " VALUES(:id,:project,:product,:bot,123456,42,"
                    "'Uncaptured Stars purchase',:payload,:charge,'paid',:at)"
                ), {"id": uuid4(), "project": project, "product": product,
                    "bot": telegram_bot_id, "payload": str(uuid4()),
                    "charge": str(uuid4()), "at": now})

                rate = (await conn.execute(text(
                    "SELECT usd_per_star FROM platform_saas_star_reward_rates "
                    "WHERE effective_on='2026-11-11'"
                ))).scalar_one()
                for kind, amount in (("file_stars", 120), ("subscription_stars", 250)):
                    reward = Decimal(amount) * Decimal(str(rate))
                    await conn.execute(text(
                        "INSERT INTO platform_saas_star_sales("
                        "id,source_kind,source_id,project_id,merchant_id,owner_user_id,"
                        "telegram_bot_id,telegram_charge_id,stars_amount,usd_per_star,"
                        "reward_usd,commission_usd,rate_effective_on,paid_at,billing_eligible)"
                        " VALUES(:id,:kind,:source,:project,:merchant,:owner,"
                        ":bot,:charge,:amount,:rate,:reward,:fee,"
                        "'2026-11-11',:at,false)"
                    ), {"id": uuid4(), "kind": kind, "source": uuid4(),
                        "project": project, "merchant": merchant, "owner": owner,
                        "bot": telegram_bot_id, "charge": str(uuid4()),
                        "amount": amount, "rate": rate, "reward": reward,
                        "fee": reward * Decimal("0.05"), "at": now})

                # Insert two real-schema Crypto Pay evidence records. Their
                # originals are TON/USDT, not USD-converted monthly commission.
                app = uuid4()
                await conn.execute(text(
                    "INSERT INTO crypto_pay_apps("
                    "id,project_id,telegram_bot_id,owner_user_id,token_ciphertext,"
                    "environment,app_label,status)"
                    " VALUES(:app,:project,:bot,:owner,'test-only','mainnet',"
                    "'Test Crypto app','active')"
                ), {"app": app, "project": project,
                    "bot": telegram_bot_id, "owner": owner})
                for kind, currency, amount, provider_id in (
                    ("file_crypto", "TON", Decimal("0.5"), 98765001),
                    ("subscription_crypto", "USDT", Decimal("4.25"), 98765002),
                ):
                    invoice = uuid4()
                    common = {"invoice": invoice, "project": project, "app": app,
                              "bot": telegram_bot_id, "amount": amount,
                              "currency": currency, "at": now,
                              "provider": provider_id}
                    await conn.execute(text(
                        "INSERT INTO crypto_pay_invoices("
                        "id,project_id,app_id,telegram_bot_id,telegram_user_id,"
                        "kind,product_id,plan_id,plan_price_id,customer_id,"
                        "name_snapshot,duration_value,duration_unit,amount,"
                        "currency_code,currency_type,payload,provider_invoice_id,"
                        "status,paid_at)"
                        " VALUES(:invoice,:project,:app,:bot,123456,"
                        ":kind,:product,:plan,:price,:customer,"
                        "'Native preview',:duration,:unit,:amount,"
                        ":currency,'crypto',:payload,:provider,'paid',:at)"
                    ), {**common, "kind": "file" if kind == "file_crypto"
                        else "subscription",
                        "product": uuid4() if kind == "file_crypto" else None,
                        "plan": uuid4() if kind == "subscription_crypto" else None,
                        "price": uuid4() if kind == "subscription_crypto" else None,
                        "customer": uuid4() if kind == "subscription_crypto" else None,
                        "duration": 1 if kind == "subscription_crypto" else None,
                        "unit": "month" if kind == "subscription_crypto" else None,
                        "payload": str(uuid4())})
                    await conn.execute(text(
                        "INSERT INTO platform_saas_crypto_sales("
                        "id,invoice_id,project_id,app_id,provider_invoice_id,"
                        "merchant_id,owner_user_id,bot_instance_id,telegram_bot_id,"
                        "source_kind,currency_code,currency_type,original_amount,"
                        "usd_per_unit,gross_usd,commission_usd,rate_source,"
                        "sale_day,paid_at,billing_eligible) "
                        "VALUES(:id,:invoice,:project,:app,:provider,"
                        ":merchant,:owner,:bot_instance,:bot,"
                        ":kind,:currency,'crypto',:amount,1,:amount,:fee,"
                        "'crypto_pay_paid_usd_rate',:day,:at,false)"
                    ), {**common, "id": uuid4(), "merchant": merchant,
                        "owner": owner, "bot_instance": bot_id,
                        "kind": kind, "day": now.astimezone(
                            ZoneInfo("Europe/Moscow")
                        ).date(), "fee": amount * Decimal("0.05")})

                # A third paid invoice has NO USD valuation/capture evidence.
                # Native turnover must still include it exactly once.
                await conn.execute(text(
                    "INSERT INTO crypto_pay_invoices("
                    "id,project_id,app_id,telegram_bot_id,telegram_user_id,"
                    "kind,product_id,name_snapshot,amount,currency_code,"
                    "currency_type,payload,provider_invoice_id,status,paid_at)"
                    " VALUES(:id,:project,:app,:bot,123456,'file',:product,"
                    "'Unvalued native Crypto',7.25,'USDT','crypto',"
                    ":payload,98765003,'paid',:at)"
                ), {"id": uuid4(), "project": project, "app": app,
                    "bot": telegram_bot_id, "product": uuid4(),
                    "payload": str(uuid4()), "at": now})

                # Runtime has no raw table grants. Read the narrowly scoped RPC.
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:id,true)"
                ), {"id": str(owner)})
                snapshot = (await conn.execute(text(
                    "SELECT public.platform_saas_bot_native_sales_preview("
                    ":project,:month) AS report"
                ).columns(report=JSONB), {
                    "project": project, "month": month,
                })).scalar_one()
                kinds = {(row["kind"], row["currency"]): row
                         for row in snapshot["rows"]}
                assert Decimal(kinds["file_manual", "RUB"]["gross"]) == 120
                assert Decimal(kinds["file_stars", "XTR"]["gross"]) == 162
                assert kinds["file_stars", "XTR"]["sold"] == 2
                assert Decimal(kinds["subscription_stars", "XTR"]["gross"]) == 250
                assert Decimal(kinds["file_crypto", "TON"]["gross"]) == Decimal("0.5")
                assert Decimal(kinds["subscription_crypto", "USDT"]["gross"]) == Decimal("4.25")
                assert Decimal(kinds["file_crypto", "USDT"]["gross"]) == Decimal("7.25")
                assert kinds["file_crypto", "USDT"]["sold"] == 1
                assert snapshot["preview_only"] is True
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:id,true)"
                ), {"id": str(outsider)})
                with pytest.raises(DBAPIError) as denied:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_bot_native_sales_preview("
                            ":project,:month)"
                        ), {"project": project, "month": month})
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                own = (await conn.execute(text(
                    "SELECT public.platform_saas_bot_native_sales_preview("
                    ":project,:month) AS report"
                ).columns(report=JSONB), {
                    "project": other_project, "month": month,
                })).scalar_one()
                assert own["rows"] == []
                await conn.execute(text("RESET ROLE"))
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()
