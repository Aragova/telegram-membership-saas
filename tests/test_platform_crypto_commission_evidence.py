"""PostgreSQL/RLS: verified Crypto Pay USD sale-date 5% evidence is dormant.

All temporary platform-operator grants, FX rates and sales evidence are
rolled back in this test. No SaaS bill or customer hold is activated.
"""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_crypto_pay_atomic_fulfillment import _issue, _shop
from test_crypto_pay_invoice_reservations import Provider, _tid

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
from telegram_membership_saas.modules.crypto_pay.client import (
    CryptoPayError,
    verify_paid_invoice,
)
from telegram_membership_saas.modules.crypto_pay.models import (
    CryptoPayInvoice,
    CryptoPayPaidUsdRate,
)
from telegram_membership_saas.modules.crypto_pay.settlement import (
    settle_crypto_pay_invoice,
)
from telegram_membership_saas.modules.file_sales.manual_models import FileManualPrice
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated real PostgreSQL and privileged audit rollback are required",
)


def test_crypto_paid_rate_is_provider_payment_time_not_current_spot():
    paid = {
        "invoice_id": 12001, "payload": "crypto-original-test",
        "currency_type": "crypto", "asset": "BTC",
        "amount": "0.005", "status": "paid",
        "paid_usd_rate": "65842.125",
        "paid_at": datetime.now(UTC).isoformat(),
    }
    values = dict(
        invoice_id=12001, payload="crypto-original-test",
        amount=Decimal("0.005"), currency_code="BTC",
        currency_type="crypto",
    )
    receipt = verify_paid_invoice(paid, **values)
    assert receipt.paid_usd_rate == Decimal("65842.125")
    assert receipt.amount == Decimal("0.005")
    assert receipt.currency_code == "BTC"
    missing = verify_paid_invoice({**paid, "paid_usd_rate": None}, **values)
    assert missing.paid_usd_rate is None  # no fake spot-rate fallback
    for invalid in (
        "0", "-1", "NaN", "99999999999",
        "0.0000000000000000001", "1e-99",
    ):
        # A tiny positive rate rounds to ZERO in SQL numeric(38,18).
        # It is optional audit data, never a reason to reject the purchase.
        unpriced = verify_paid_invoice(
            {**paid, "paid_usd_rate": invalid}, **values,
        )
        assert unpriced.paid_usd_rate is None
    normalized = verify_paid_invoice(
        {**paid, "paid_usd_rate": "2.1234567890123456789"}, **values,
    )
    assert normalized.paid_usd_rate == Decimal("2.123456789012345679")
    smallest = verify_paid_invoice(
        {**paid, "paid_usd_rate": "0.000000000000000001"}, **values,
    )
    assert smallest.paid_usd_rate == Decimal("0.000000000000000001")
    with pytest.raises(CryptoPayError):
        verify_paid_invoice({**paid, "invoice_id": 777}, **values)
    fiat = verify_paid_invoice({
        **paid, "currency_type": "fiat", "fiat": "USD",
        "amount": "4.00",
    }, **{**values, "currency_type": "fiat",
           "currency_code": "USD", "amount": Decimal("4")})
    assert fiat.paid_usd_rate is None


async def test_crypto_fiat_usd_and_rub_sale_evidence_is_owner_only_and_idempotent():
    Provider.created = []
    Provider.invoice_data = {}
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        owner, project_id, bot_id, product_id = await _shop(factory, key)
        operator_tg = _tid()
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        # Two real getInvoices-verified paid purchases, different buyers.
        rub_invoice = await _issue(
            factory, project=project_id, bot_id=bot_id,
            buyer=_tid(), kind="file", target_id=product_id, key=key,
        )
        async with system_project_session_scope(factory, project_id) as session:
            session.add(FileManualPrice(
                project_id=project_id, product_id=product_id,
                currency_code="USD", amount=Decimal("20.00"),
            ))
        from telegram_membership_saas.modules.crypto_pay.checkout import (
            issue_reserved_crypto_invoice,
            reserve_crypto_invoice,
        )
        second_buyer = _tid()
        usd_invoice = await reserve_crypto_invoice(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=second_buyer, kind="file", target_id=product_id,
            currency="USD",
        )
        await issue_reserved_crypto_invoice(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=second_buyer, invoice_id=usd_invoice,
            encryption_key=key, client_factory=Provider,
        )
        for invoice in (rub_invoice, usd_invoice):
            paid = await settle_crypto_pay_invoice(
                factory, project_id=project_id, invoice_id=invoice,
                encryption_key=key, client_factory=Provider,
            )
            assert paid.changed

        sale_date = datetime.now(UTC).astimezone(
            ZoneInfo("Europe/Moscow")
        ).date()
        month = sale_date.replace(day=1)
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                # _shop's test-only app is testnet; emulate a trusted
                # mainnet merchant credential inside rollback-only fixture.
                await conn.execute(text(
                    "UPDATE crypto_pay_apps SET environment='mainnet' "
                    "WHERE project_id=:project"
                ), {"project": project_id})
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": operator_tg})
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(owner)})
                with pytest.raises(DBAPIError) as rejected:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT public.platform_saas_capture_crypto_sales(200)"
                        ))
                assert getattr(rejected.value.orig, "sqlstate", None) == "42501"
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(operator)})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                # Use the existing immutable official rate (or publish a
                # fixture via the already-gated verified CBR owner function).
                daily = (await conn.execute(text(
                    "SELECT rub_per_unit FROM platform_saas_daily_fx_rates "
                    "WHERE rate_date=:day AND currency_code='USD'"
                ), {"day": sale_date})).scalar_one_or_none()
                if daily is None:
                    await conn.execute(text(
                        "SELECT public.platform_saas_publish_cbr_daily_fx("
                        ":day,'USD',90,:day)"
                    ), {"day": sale_date})
                    daily = Decimal("90")
                capture = text(
                    "SELECT public.platform_saas_capture_crypto_sales("
                    "200) AS data"
                ).columns(data=JSONB)
                first = (await conn.execute(capture)).scalar_one()
                assert first["recorded"] >= 2
                assert first["billing_activated"] is False
                assert first["invoices_issued"] is False
                assert (await conn.execute(capture)).scalar_one()["recorded"] == 0
                with pytest.raises(DBAPIError) as denied:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "SELECT * FROM platform_saas_crypto_sales"
                        ))
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(owner)})
                result = (await conn.execute(text(
                    "SELECT public.platform_saas_crypto_project_summary("
                    ":project,:month) AS data"
                ).columns(data=JSONB), {
                    "project": project_id, "month": month,
                })).scalar_one()
                assert result["sold"] == 2
                assert result["pending_valuation"] == 0
                assert result["preview_only"] and not result["invoices_issued"]
                gross = (
                    Decimal("20.00") + Decimal("55") / Decimal(daily)
                )
                # The RUB-per-USD rate is first reduced to numeric(38,18).
                assert abs(Decimal(result["gross_usd"]) - gross) < Decimal("1e-10")
                assert abs(Decimal(result["commission_usd"])
                           - gross * Decimal("0.05")) < Decimal("1e-10")
                assert Decimal(result["billable_commission_usd"]) == 0
                assert len(result["rows"]) == 2
                await conn.execute(text("RESET ROLE"))
                evidence = (await conn.execute(text(
                    "SELECT invoice_id,currency_code,rate_source,"
                    "billing_eligible FROM platform_saas_crypto_sales "
                    "WHERE invoice_id IN (:rub,:usd)"
                ), {"rub": rub_invoice, "usd": usd_invoice})).all()
                assert len(evidence) == 2
                sources = {row.currency_code: row.rate_source for row in evidence}
                assert sources == {"RUB": "cbr_sale_day", "USD": "fiat_usd"}
                assert not any(row.billing_eligible for row in evidence)
                with pytest.raises(DBAPIError) as immutable:
                    async with conn.begin_nested():
                        await conn.execute(text(
                            "UPDATE platform_saas_crypto_sales "
                            "SET billing_eligible=true WHERE invoice_id=:id"
                        ), {"id": rub_invoice})
                assert getattr(immutable.value.orig, "sqlstate", None) == "42501"
                enabled = (await conn.execute(text(
                    "SELECT enabled FROM platform_saas_billing_control WHERE id=1"
                ))).scalar_one()
                assert enabled is False
            finally:
                await tx.rollback()
    finally:
        await admin.dispose()
        await engine.dispose()



class CryptoAssetProvider(Provider):
    """Actual crypto-denominated getInvoices fixture with original USD rate."""
    created = []
    invoice_data = {}
    paid_rate = "3.1234567890123456789"

    async def create_invoice(self, *, currency, amount, payload, description):
        self.created.append((self.token, currency, amount, payload, description))
        provider_id = len(self.created) + 41000
        self.invoice_data[provider_id] = {
            "invoice_id": provider_id,
            "payload": payload,
            "currency_type": "crypto",
            "asset": currency,
            "amount": format(amount, "f"),
            "status": "paid",
            "paid_at": datetime.now(UTC).isoformat(),
            "paid_usd_rate": self.paid_rate,
        }
        return {
            **self.invoice_data[provider_id],
            "status": "active",
            "bot_invoice_url": "https://t.me/CryptoTestnetBot?start=IV321",
        }


async def test_crypto_paid_asset_rate_persists_atomically_and_replays_safely():
    """Real PostgreSQL snapshot belongs to original verified paid invoice."""
    CryptoAssetProvider.created = []
    CryptoAssetProvider.invoice_data = {}
    CryptoAssetProvider.paid_rate = "3.1234567890123456789"
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        _, project_id, bot_id, product_id = await _shop(factory, key)
        async with system_project_session_scope(factory, project_id) as session:
            session.add(FileManualPrice(
                project_id=project_id, product_id=product_id,
                currency_code="TON", amount=Decimal("1.5"),
            ))
        first_buyer = _tid()
        local_id = await reserve_crypto_invoice(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=first_buyer, kind="file",
            target_id=product_id, currency="TON",
        )
        await issue_reserved_crypto_invoice(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=first_buyer, invoice_id=local_id,
            encryption_key=key, client_factory=CryptoAssetProvider,
        )
        result = await settle_crypto_pay_invoice(
            factory, project_id=project_id, invoice_id=local_id,
            encryption_key=key, client_factory=CryptoAssetProvider,
        )
        replay = await settle_crypto_pay_invoice(
            factory, project_id=project_id, invoice_id=local_id,
            encryption_key=key, client_factory=CryptoAssetProvider,
        )
        assert result.changed and not replay.changed
        async with system_project_session_scope(factory, project_id) as session:
            paid = await session.get(CryptoPayInvoice, local_id)
            quote = await session.get(CryptoPayPaidUsdRate, local_id)
            assert paid.status == "paid"
            assert quote is not None and quote.asset_code == "TON"
            assert quote.usd_per_asset == Decimal("3.123456789012345679")
            assert quote.paid_at == paid.paid_at

        # The real payment and file access must STILL settle when the
        # provider's optional paid USD quote underflows DB precision.
        CryptoAssetProvider.paid_rate = "0.0000000000000000001"
        second_buyer = _tid()
        unpriced_id = await reserve_crypto_invoice(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=second_buyer, kind="file",
            target_id=product_id, currency="TON",
        )
        await issue_reserved_crypto_invoice(
            factory, project_id=project_id, bot_id=bot_id,
            buyer_id=second_buyer, invoice_id=unpriced_id,
            encryption_key=key, client_factory=CryptoAssetProvider,
        )
        paid = await settle_crypto_pay_invoice(
            factory, project_id=project_id, invoice_id=unpriced_id,
            encryption_key=key, client_factory=CryptoAssetProvider,
        )
        assert paid.changed
        async with system_project_session_scope(factory, project_id) as session:
            invoice = await session.get(CryptoPayInvoice, unpriced_id)
            quote = await session.get(CryptoPayPaidUsdRate, unpriced_id)
            assert invoice.status == "paid"
            assert quote is None  # unresolved finance, valid purchase
    finally:
        await engine.dispose()



async def test_crypto_audit_cursor_wraps_even_as_new_paid_invoices_arrive():
    """A fixed high-water mark retries old unpriced sales under steady traffic.

    Synthetic provider-paid rows exist ONLY within this privileged rollback
    transaction to exercise the SQL scan protocol. No real payment is made.
    """
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    try:
        owner, project_id, bot_id, product_id = await _shop(
            factory, SecretStr(Fernet.generate_key().decode("ascii")),
        )
        operator_tg = _tid()
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg,
            username=None, first_name=None,
        )
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                app_id = (await conn.execute(text(
                    "SELECT id FROM crypto_pay_apps WHERE project_id=:project"
                ), {"project": project_id})).scalar_one()
                await conn.execute(text(
                    "UPDATE crypto_pay_apps SET environment='mainnet' "
                    "WHERE id=:id"
                ), {"id": app_id})
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": operator_tg})
                cutoff = (await conn.execute(text(
                    "SELECT started_at FROM platform_saas_crypto_capture_control"
                    " WHERE id=1"
                ))).scalar_one()
                # Different timestamps ensure no rows appear between A and B.
                # All are newer than the audit capture cutoff, older than
                # normal test-created sales (migrations happen first).
                when_a = cutoff + timedelta(milliseconds=1)
                when_b = cutoff + timedelta(milliseconds=2)
                when_c = cutoff + timedelta(milliseconds=3)
                invoice_a, invoice_b, invoice_c = uuid4(), uuid4(), uuid4()

                async def insert_unpriced_invoice(invoice, when, number):
                    await conn.execute(text(
                        "INSERT INTO crypto_pay_invoices("
                        "id,project_id,app_id,telegram_bot_id,telegram_user_id,"
                        "kind,product_id,name_snapshot,amount,currency_code,"
                        "currency_type,payload,provider_invoice_id,status,paid_at"
                        ") VALUES("
                        ":id,:project,:app,:bot,:buyer,'file',:product,"
                        "'Audit test',1,'TON','crypto',:payload,:provider,"
                        "'paid',:paid)"
                    ), {
                        "id": invoice, "project": project_id,
                        "app": app_id, "bot": bot_id, "buyer": _tid(),
                        "product": product_id,
                        "payload": "cp:" + invoice.hex,
                        "provider": 54000 + number,
                        "paid": when,
                    })

                await insert_unpriced_invoice(invoice_a, when_a, 1)
                await insert_unpriced_invoice(invoice_b, when_b, 2)
                # Simulate a scan in progress: A was visited but its USD
                # quote was unavailable; B is the frozen end of this pass.
                await conn.execute(text(
                    "UPDATE platform_saas_crypto_scan_cursor "
                    "SET last_paid_at=:a,last_invoice_id=:aid,"
                    "max_paid_at=:b,max_invoice_id=:bid WHERE id=1"
                ), {
                    "a": when_a, "aid": invoice_a,
                    "b": when_b, "bid": invoice_b,
                })
                # Continuous new traffic beyond the original high-water
                # bound must NOT prevent returning to A later.
                await insert_unpriced_invoice(invoice_c, when_c, 3)
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:actor,true)"
                ), {"actor": str(operator)})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                rpc = text(
                    "SELECT public.platform_saas_capture_crypto_sales(1) AS data"
                ).columns(data=JSONB)
                first = (await conn.execute(rpc)).scalar_one()
                assert first["examined"] == 1
                assert first["unresolved"] == 1
                await conn.execute(text("RESET ROLE"))
                cursor = (await conn.execute(text(
                    "SELECT last_paid_at,last_invoice_id,max_paid_at,max_invoice_id "
                    "FROM platform_saas_crypto_scan_cursor WHERE id=1"
                ))).one()
                assert all(item is None for item in cursor)
                # The very next scan restarts from old unpriced A despite C
                # being newer and still outstanding: no idle period required.
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                second = (await conn.execute(rpc)).scalar_one()
                assert second["examined"] == 1
                assert second["unresolved"] == 1
                await conn.execute(text("RESET ROLE"))
                restarted = (await conn.execute(text(
                    "SELECT last_invoice_id,max_paid_at,max_invoice_id "
                    "FROM platform_saas_crypto_scan_cursor WHERE id=1"
                ))).one()
                assert restarted.last_invoice_id == invoice_a
                # Other legitimate mainnet fixtures in the shared CI
                # database may have a later paid_at than source C. The
                # horizon must INCLUDE C, not necessarily end AT C.
                assert restarted.max_paid_at >= when_c
                assert restarted.max_invoice_id is not None
                assert (await conn.execute(text(
                    "SELECT COUNT(*) FROM platform_saas_crypto_sales "
                    "WHERE project_id=:project"
                ), {"project": project_id})).scalar_one() == 0
            finally:
                await tx.rollback()
    finally:
        await admin.dispose()
        await engine.dispose()
