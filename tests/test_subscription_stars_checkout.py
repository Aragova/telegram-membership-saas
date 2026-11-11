"""Stars plans: real PostgreSQL payment -> subscription -> access outbox.

Every buyer action is under a real project scope. Same charge retries grant
exactly once; wrong buyer/amount/bot cannot deliver. Manual settlement remains.
"""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import StarTransaction
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.deletion_service import (
    count_retirement_blockers,
)
from telegram_membership_saas.modules.entitlements import EntitlementGrant
from telegram_membership_saas.modules.outbox import OutboxEvent
from telegram_membership_saas.modules.owner_setup.service import (
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import replace_active_plan_price
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.modules.stars_subscription_recovery import (
    reconcile_subscription_star_ledger,
)
from telegram_membership_saas.modules.stars_subscription_refunds import (
    record_verified_subscription_star_refund,
)
from telegram_membership_saas.modules.stars_subscription_service import (
    confirm_subscription_stars_payment,
    create_stars_subscription_order,
    stars_subscription_precheckout,
)
from telegram_membership_saas.modules.stars_subscriptions import (
    SubscriptionStarOrder,
    SubscriptionStarRefund,
)
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL")
    or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL",
)


async def test_native_stars_charge_grants_one_subscription_and_access():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    buyer_id = _tid()
    try:
        owner, _, project_id, bot_uuid, _ = await _project(factory, _tid())
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id),
        ) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
            channel = await session.scalar(select(Resource).where(
                Resource.project_id == project_id,
            ))
            plan = Plan(
                project_id=project_id, name="One month Stars",
                status="active", duration_value=1, duration_unit="month",
            )
            session.add(plan)
            await session.flush()
            session.add(PlanResource(
                project_id=project_id, plan_id=plan.id,
                resource_id=channel.id,
            ))
            price = await replace_active_plan_price(
                session, project_id=project_id, plan_id=plan.id,
                currency_code="XTR", amount="2000",
            )
            price_id = price.id
        async with system_project_session_scope(factory, project_id) as session:
            order = await create_stars_subscription_order(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, username=None, first_name="Buyer",
                plan_price_id=price_id,
            )
            payload, order_id = order.invoice_payload, order.id
        async with system_project_session_scope(factory, project_id) as session:
            assert await stars_subscription_precheckout(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=payload, currency="XTR",
                amount=2000,
            )
            assert not await stars_subscription_precheckout(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=payload, currency="XTR",
                amount=1000,
            )
        moment = datetime.now(UTC)
        async with system_project_session_scope(factory, project_id) as session:
            with pytest.raises(PermissionError):
                await confirm_subscription_stars_payment(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer_id + 1, payload=payload,
                    currency="XTR", amount=2000, charge_id="correct",
                    paid_at=moment,
                )
        async with system_project_session_scope(factory, project_id) as session:
            paid, subscription = await confirm_subscription_stars_payment(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=payload,
                currency="XTR", amount=2000, charge_id="paid-starter",
                paid_at=moment,
            )
            assert paid.status == "paid"
            assert subscription.last_payment_id is None
            assert subscription.last_star_order_id == order_id
            assert subscription.current_period_end > moment
        async with system_project_session_scope(factory, project_id) as session:
            repeat, renewed = await confirm_subscription_stars_payment(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=payload,
                currency="XTR", amount=2000, charge_id="paid-starter",
                paid_at=moment,
            )
            assert repeat.id == order_id
            assert renewed.last_star_order_id == order_id
            assert (await session.scalar(select(func.count()).select_from(
                EntitlementGrant,
            ).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source == "stars_payment",
                EntitlementGrant.source_id == order_id,
            ))) == 1
            assert (await session.scalar(select(func.count()).select_from(
                OutboxEvent,
            ).where(
                OutboxEvent.project_id == project_id,
                OutboxEvent.aggregate_type == "entitlement_grant",
            ))) == 1
            assert (await session.scalar(select(func.count()).select_from(
                Subscription,
            ).where(
                Subscription.project_id == project_id,
                Subscription.customer_id == renewed.customer_id,
                Subscription.plan_id == plan.id,
            ))) == 1
            with pytest.raises(PermissionError):
                await confirm_subscription_stars_payment(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer_id, payload=payload,
                    currency="XTR", amount=2000, charge_id="different",
                    paid_at=moment,
                )
        async with system_project_session_scope(factory, project_id) as session:
            before = await count_retirement_blockers(
                session, project_id=project_id,
            )
            assert before.subscriptions >= 1
            with pytest.raises(PermissionError):
                await record_verified_subscription_star_refund(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer_id, payload=payload,
                    currency="XTR", amount=1999,
                    charge_id="paid-starter", refunded_at=moment,
                )
        async with system_project_session_scope(factory, project_id) as session:
            refund_id = await record_verified_subscription_star_refund(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=payload,
                currency="XTR", amount=2000,
                charge_id="paid-starter", refunded_at=moment,
            )
            assert refund_id is not None
        async with system_project_session_scope(factory, project_id) as session:
            repeated_id = await record_verified_subscription_star_refund(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=payload,
                currency="XTR", amount=2000,
                charge_id="paid-starter", refunded_at=moment,
            )
            assert repeated_id == refund_id
            assert (await session.scalar(select(func.count()).select_from(
                SubscriptionStarRefund,
            ).where(
                SubscriptionStarRefund.project_id == project_id,
                SubscriptionStarRefund.order_id == order_id,
            ))) == 1
            assert (await session.scalar(select(func.count()).select_from(
                EntitlementGrant,
            ).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == order_id,
                EntitlementGrant.status == "active",
            ))) == 0
            assert (await session.scalar(select(Subscription.status).where(
                Subscription.project_id == project_id,
                Subscription.last_star_order_id == order_id,
            ))) == "cancelled"
            assert (await session.scalar(select(SubscriptionStarOrder.status).where(
                SubscriptionStarOrder.project_id == project_id,
                SubscriptionStarOrder.id == order_id,
            ))) == "paid"
    finally:
        await engine.dispose()


async def test_refund_newest_renewal_and_lost_ledger_refund_preserve_old_access():
    """An earlier paid grant survives the latest renewal's refund."""
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    buyer = _tid()
    try:
        owner, _, project_id, bot_uuid, _ = await _project(factory, _tid())
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id),
        ) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
            resource_id = await session.scalar(select(Resource.id).where(
                Resource.project_id == project_id,
            ))
            plan = Plan(
                project_id=project_id, name="Test renewal",
                status="active", duration_value=1, duration_unit="month",
            )
            session.add(plan)
            await session.flush()
            session.add(PlanResource(
                project_id=project_id, plan_id=plan.id,
                resource_id=resource_id,
            ))
            price = await replace_active_plan_price(
                session, project_id=project_id,
                plan_id=plan.id, currency_code="XTR", amount="70",
            )
            price_id = price.id
        moment = datetime.now(UTC)
        ids = []
        for index in range(2):
            async with system_project_session_scope(factory, project_id) as session:
                order = await create_stars_subscription_order(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer, username=None, first_name="Buyer",
                    plan_price_id=price_id,
                )
                payload, order_id = order.invoice_payload, order.id
            async with system_project_session_scope(factory, project_id) as session:
                _, sub = await confirm_subscription_stars_payment(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer, payload=payload, currency="XTR",
                    amount=70, charge_id=f"renewal-{index}",
                    paid_at=moment,
                )
                ids.append((payload, order_id, sub.current_period_end))
        assert ids[1][2] > ids[0][2]

        refund = SimpleNamespace(
            id="renewal-1",
            amount=70,
            date=moment,
            source=None,
            receiver=SimpleNamespace(
                type="user", transaction_type="invoice_payment",
                user=SimpleNamespace(id=buyer),
                invoice_payload=ids[1][0],
            ),
        )
        bot = SimpleNamespace(
            id=bot_id,
            get_star_transactions=AsyncMock(return_value=SimpleNamespace(
                transactions=[refund],
            )),
        )
        purchased, returned = await reconcile_subscription_star_ledger(
            factory, bot=bot, project_id=project_id, buyer_id=buyer,
            pages=1,
        )
        assert not purchased and len(returned) == 1
        async with system_project_session_scope(factory, project_id) as session:
            sub = await session.scalar(select(Subscription).where(
                Subscription.project_id == project_id,
                Subscription.plan_id == plan.id,
            ))
            assert sub.status == "active"
            assert sub.current_period_end == ids[0][2]
            assert sub.last_star_order_id == ids[0][1]
            assert (await session.scalar(select(func.count()).select_from(
                EntitlementGrant,
            ).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == ids[0][1],
                EntitlementGrant.status == "active",
            ))) == 1
            assert (await session.scalar(select(func.count()).select_from(
                EntitlementGrant,
            ).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == ids[1][1],
                EntitlementGrant.status == "active",
            ))) == 0

        # The Bot API returns an outgoing refund with positive amount and
        # receiver (not a negative amount as TDLib internally represents).
        # Telegram's refund and the incoming Stars payment
        # may both arrive while PostgreSQL is unavailable. Replay both from
        # Telegram's own ledger, not user text or a screenshot.
        async with system_project_session_scope(factory, project_id) as session:
            order3 = await create_stars_subscription_order(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer, username=None, first_name="Buyer",
                plan_price_id=price_id,
            )
            third_id, third_payload = order3.id, order3.invoice_payload
        source = SimpleNamespace(
            type="user", transaction_type="invoice_payment",
            user=SimpleNamespace(id=buyer),
            invoice_payload=third_payload,
        )
        positive = SimpleNamespace(
            id="renewal-2", amount=70, date=moment,
            source=source, receiver=None,
        )
        negative = SimpleNamespace(
            id="renewal-2", amount=70, date=moment,
            source=None,
            receiver=SimpleNamespace(
                type="user", transaction_type="invoice_payment",
                user=SimpleNamespace(id=buyer),
                invoice_payload=third_payload,
            ),
        )
        bot.get_star_transactions.return_value = SimpleNamespace(
            transactions=[positive, negative],
        )
        purchased, returned = await reconcile_subscription_star_ledger(
            factory, bot=bot, project_id=project_id, buyer_id=buyer,
            pages=1,
        )
        assert third_id in purchased
        assert len(returned) == 1
        async with system_project_session_scope(factory, project_id) as session:
            sub = await session.scalar(select(Subscription).where(
                Subscription.project_id == project_id,
                Subscription.plan_id == plan.id,
            ))
            assert sub.status == "active"
            assert sub.current_period_end == ids[0][2]
            assert sub.last_star_order_id == ids[0][1]
            assert (await session.scalar(select(func.count()).select_from(
                SubscriptionStarRefund,
            ).where(SubscriptionStarRefund.project_id == project_id))) == 2
    finally:
        await engine.dispose()


async def test_replay_refund_before_next_purchase_does_not_delay_access():
    """Telegram returns latest first: A paid, A refunded, B paid is replayed
    chronologically, so B starts from its own purchase date, not refunded A.
    """
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    buyer = _tid()
    try:
        owner, _, project_id, bot_uuid, _ = await _project(factory, _tid())
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id),
        ) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
            resource_id = await session.scalar(select(Resource.id).where(
                Resource.project_id == project_id,
            ))
            plan = Plan(
                project_id=project_id, name="Chronological Stars",
                status="active", duration_value=1, duration_unit="month",
            )
            session.add(plan)
            await session.flush()
            session.add(PlanResource(
                project_id=project_id, plan_id=plan.id,
                resource_id=resource_id,
            ))
            price = await replace_active_plan_price(
                session, project_id=project_id,
                plan_id=plan.id, currency_code="XTR", amount="70",
            )
            price_id = price.id
        payloads = []
        for _ in range(2):
            async with system_project_session_scope(factory, project_id) as session:
                order = await create_stars_subscription_order(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer, username=None, first_name="Buyer",
                    plan_price_id=price_id,
                )
                payloads.append((order.id, order.invoice_payload))
        t0 = (datetime.now(UTC) - timedelta(minutes=3)).replace(
            microsecond=0
        )
        refund_at = t0 + timedelta(minutes=1)
        b_paid_at = t0 + timedelta(minutes=2)

        def _paid(payload, charge, moment):
            return SimpleNamespace(
                id=charge, amount=70, date=moment, receiver=None,
                source=SimpleNamespace(
                    type="user", transaction_type="invoice_payment",
                    user=SimpleNamespace(id=buyer), invoice_payload=payload,
                ),
            )

        def _refund(payload, charge, moment):
            # Parse the real Bot API object through aiogram. The outgoing
            # refund carries a POSITIVE amount and receiver, no source.
            return StarTransaction.model_validate({
                "id": charge, "amount": 70,
                "date": int(moment.timestamp()),
                "receiver": {
                    "type": "user",
                    "transaction_type": "invoice_payment",
                    "user": {
                        "id": buyer, "is_bot": False,
                        "first_name": "Buyer",
                    },
                    "invoice_payload": payload,
                },
            })

        # Telegram ledger can be newest-first. The real Bot API serializes
        # a positive refund amount with receiver and no source. A naive
        # two-pass collector would wrongly extend B after refunded A.
        bot = SimpleNamespace(
            id=bot_id,
            get_star_transactions=AsyncMock(return_value=SimpleNamespace(
                transactions=[
                    _paid(payloads[1][1], "charge-B", b_paid_at),
                    _refund(payloads[0][1], "charge-A", refund_at),
                    _paid(payloads[0][1], "charge-A", t0),
                ],
            )),
        )
        paid, returned = await reconcile_subscription_star_ledger(
            factory, bot=bot, project_id=project_id, buyer_id=buyer,
            pages=1,
        )
        assert set(paid) == {payloads[0][0], payloads[1][0]}
        assert len(returned) == 1
        async with system_project_session_scope(factory, project_id) as session:
            sub = await session.scalar(select(Subscription).where(
                Subscription.project_id == project_id,
                Subscription.plan_id == plan.id,
            ))
            assert sub.status == "active"
            assert sub.last_star_order_id == payloads[1][0]
            assert sub.current_period_start == b_paid_at
            assert (await session.scalar(select(EntitlementGrant.valid_from).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == payloads[1][0],
            ))) == b_paid_at
            assert (await session.scalar(select(func.count()).select_from(
                EntitlementGrant,
            ).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == payloads[0][0],
                EntitlementGrant.status == "active",
            ))) == 0
    finally:
        await engine.dispose()


async def test_merchant_bill_includes_subscription_stars_refund_commission():
    """One genuine refunded subscription appears in the merchant's single
    monthly refund section, instead of only under file-sales refunds.
    """
    cfg = Settings()
    engine = build_engine(cfg)
    factory = build_session_factory(engine)
    admin = create_async_engine(cfg.migration_database_url)
    operator_tg = _tid()
    try:
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg, username=None, first_name=None,
        )
        owner, _, project_id, bot_uuid, _ = await _project(factory, _tid())
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id),
        ) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
            resource_id = await session.scalar(select(Resource.id).where(
                Resource.project_id == project_id,
            ))
            plan = Plan(
                project_id=project_id, name="Stars merchant refund",
                status="active", duration_value=1, duration_unit="month",
            )
            session.add(plan)
            await session.flush()
            session.add(PlanResource(
                project_id=project_id, plan_id=plan.id,
                resource_id=resource_id,
            ))
            price = await replace_active_plan_price(
                session, project_id=project_id,
                plan_id=plan.id, currency_code="XTR", amount="70",
            )
            price_id = price.id
        buyer_id = _tid()
        async with system_project_session_scope(factory, project_id) as session:
            order = await create_stars_subscription_order(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, username=None, first_name="Buyer",
                plan_price_id=price_id,
            )
            order_id, payload = order.id, order.invoice_payload
        now = datetime.now(UTC)
        async with system_project_session_scope(factory, project_id) as session:
            await confirm_subscription_stars_payment(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=payload, currency="XTR",
                amount=70, charge_id="merchant-" + str(order_id),
                paid_at=now,
            )
        async with system_project_session_scope(factory, project_id) as session:
            await record_verified_subscription_star_refund(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=payload, currency="XTR",
                amount=70, charge_id="merchant-" + str(order_id),
                refunded_at=now,
            )
        month = now.astimezone(ZoneInfo("Europe/Moscow")).date().replace(day=1)
        async with admin.connect() as conn:
            tx = await conn.begin()
            try:
                await conn.execute(text(
                    "INSERT INTO platform_operators(telegram_user_id,role)"
                    " VALUES(:tg,'owner')"
                ), {"tg": operator_tg})
                await conn.execute(text("SET LOCAL ROLE membership_runtime"))
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:user,true)"
                ), {"user": str(operator)})
                captured = (await conn.execute(text(
                    "SELECT platform_saas_capture_new_subscription_stars(200)"
                    " AS data"
                ).columns(data=JSONB))).scalar_one()
                assert captured["recorded"] >= 1
                refunds = (await conn.execute(text(
                    "SELECT platform_saas_capture_subscription_star_refunds(200)"
                    " AS data"
                ).columns(data=JSONB))).scalar_one()
                assert refunds["recorded"] >= 1
                assert refunds["applied_to_invoices"] is False
                await conn.execute(text(
                    "SELECT set_config('app.current_user_id',:user,true)"
                ), {"user": str(owner)})
                report = (await conn.execute(text(
                    "SELECT platform_saas_star_project_refund_summary("
                    ":project,:month) AS data"
                ).columns(data=JSONB), {
                    "project": project_id, "month": month,
                })).scalar_one()
                assert report["refunds"] == 1
                assert report["refunded_stars"] == 70
                assert Decimal(report["reward_credit_usd"]) == Decimal("0.91")
                assert Decimal(report["commission_credit_usd"]) == Decimal(
                    "0.0455"
                )
                assert report["pending_refund_reconciliation"] == 0
                assert report["applied_to_invoices"] is False
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()
        await admin.dispose()


async def test_delayed_refund_repairs_already_confirmed_newer_stars_purchase():
    """B paid after A was refunded, but B's successful_payment reached DB
    before A's delayed refunded_payment. B must not wait for A's old expiry.
    """
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    buyer_id = _tid()
    try:
        owner, _, project_id, bot_uuid, _ = await _project(factory, _tid())
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id),
        ) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
            resource_id = await session.scalar(select(Resource.id).where(
                Resource.project_id == project_id,
            ))
            plan = Plan(
                project_id=project_id, name="Late refund already-paid renewal",
                status="active", duration_value=1, duration_unit="month",
            )
            session.add(plan)
            await session.flush()
            session.add(PlanResource(
                project_id=project_id, plan_id=plan.id,
                resource_id=resource_id,
            ))
            price = await replace_active_plan_price(
                session, project_id=project_id, plan_id=plan.id,
                currency_code="XTR", amount="70",
            )
            price_id = price.id
        paid_a = (datetime.now(UTC) - timedelta(minutes=5)).replace(
            microsecond=0
        )
        returned_a = paid_a + timedelta(minutes=1)
        paid_b = paid_a + timedelta(minutes=2)
        receipts = []
        for index, paid_at in enumerate((paid_a, paid_b)):
            async with system_project_session_scope(factory, project_id) as session:
                order = await create_stars_subscription_order(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer_id, username=None, first_name="Buyer",
                    plan_price_id=price_id,
                )
                order_id, payload = order.id, order.invoice_payload
            async with system_project_session_scope(factory, project_id) as session:
                await confirm_subscription_stars_payment(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer_id, payload=payload, currency="XTR",
                    amount=70, charge_id=f"delayed-refund-{index}",
                    paid_at=paid_at,
                )
            receipts.append((order_id, payload))
        async with system_project_session_scope(factory, project_id) as session:
            before = await session.scalar(select(EntitlementGrant.valid_from).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == receipts[1][0],
            ))
            assert before > paid_b  # B wrongly stacked on still-active A.
        async with system_project_session_scope(factory, project_id) as session:
            refund_id = await record_verified_subscription_star_refund(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=receipts[0][1],
                amount=70, currency="XTR", charge_id="delayed-refund-0",
                refunded_at=returned_a,
            )
            assert refund_id is not None
        async with system_project_session_scope(factory, project_id) as session:
            sub = await session.scalar(select(Subscription).where(
                Subscription.project_id == project_id,
                Subscription.plan_id == plan.id,
            ))
            later = await session.scalar(select(EntitlementGrant).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == receipts[1][0],
            ))
            assert later.status == "active"
            assert later.valid_from == paid_b
            assert later.valid_until == sub.current_period_end
            assert sub.current_period_start == paid_b
            assert sub.last_star_order_id == receipts[1][0]
            assert sub.status == "active"
            assert (await session.scalar(select(func.count()).select_from(
                EntitlementGrant,
            ).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == receipts[0][0],
                EntitlementGrant.status == "active",
            ))) == 0
        # Duplicate Telegram refund is idempotent after both grant repair
        # and owner-source financial evidence.
        async with system_project_session_scope(factory, project_id) as session:
            same_id = await record_verified_subscription_star_refund(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer_id, payload=receipts[0][1],
                amount=70, currency="XTR", charge_id="delayed-refund-0",
                refunded_at=returned_a,
            )
            assert same_id == refund_id
            assert (await session.scalar(select(EntitlementGrant.valid_from).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == receipts[1][0],
            ))) == paid_b
    finally:
        await engine.dispose()


async def test_old_stars_charge_recovered_after_newer_paid_purchase():
    """An old day-long charge must not be appended after a newer renewal."""
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    buyer = _tid()
    try:
        owner, _, project_id, bot_uuid, _ = await _project(factory, _tid())
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id),
        ) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
            resource_id = await session.scalar(select(Resource.id).where(
                Resource.project_id == project_id,
            ))
            plan = Plan(
                project_id=project_id, name="Historical Stars recovery",
                status="active", duration_value=1, duration_unit="day",
            )
            session.add(plan)
            await session.flush()
            session.add(PlanResource(
                project_id=project_id, plan_id=plan.id, resource_id=resource_id,
            ))
            price = await replace_active_plan_price(
                session, project_id=project_id, plan_id=plan.id,
                currency_code="XTR", amount="70",
            )
            price_id = price.id
        receipts = []
        for _ in range(2):
            async with system_project_session_scope(factory, project_id) as session:
                order = await create_stars_subscription_order(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer, username=None, first_name="Buyer",
                    plan_price_id=price_id,
                )
                receipts.append((order.id, order.invoice_payload))
        now = datetime.now(UTC).replace(microsecond=0)
        older = now - timedelta(days=3)
        newer = now - timedelta(hours=12)
        # Newer charge was already processed by a successful_payment update.
        async with system_project_session_scope(factory, project_id) as session:
            await confirm_subscription_stars_payment(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer, payload=receipts[1][1], currency="XTR",
                amount=70, charge_id="chron-new", paid_at=newer,
            )
        # The older paid charge only arrives via authenticated ledger recovery.
        async with system_project_session_scope(factory, project_id) as session:
            await confirm_subscription_stars_payment(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer, payload=receipts[0][1], currency="XTR",
                amount=70, charge_id="chron-old", paid_at=older,
            )
        async with system_project_session_scope(factory, project_id) as session:
            old_grant = await session.scalar(select(EntitlementGrant).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == receipts[0][0],
            ))
            new_grant = await session.scalar(select(EntitlementGrant).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == receipts[1][0],
            ))
            sub = await session.scalar(select(Subscription).where(
                Subscription.project_id == project_id, Subscription.plan_id == plan.id,
            ))
            assert old_grant.valid_from == older
            assert old_grant.valid_until == older + timedelta(days=1)
            assert new_grant.valid_from == newer
            assert new_grant.valid_until == newer + timedelta(days=1)
            assert sub.last_star_order_id == receipts[1][0]
            assert sub.current_period_end == newer + timedelta(days=1)
            assert sub.status == "active"
        # The same recovered charge cannot append another period.
        async with system_project_session_scope(factory, project_id) as session:
            await confirm_subscription_stars_payment(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer, payload=receipts[0][1], currency="XTR",
                amount=70, charge_id="chron-old", paid_at=older,
            )
            assert (await session.scalar(select(func.count()).select_from(
                EntitlementGrant,
            ).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == receipts[0][0],
            ))) == 1
    finally:
        await engine.dispose()


def test_manual_anchor_uses_payment_approval_not_future_grant_start():
    """A prepaid manual renewal remains an anchor for later Stars purchase."""
    from telegram_membership_saas.modules.stars_subscription_timeline import (
        historical_stars_start,
        manual_anchor_end,
    )
    now = datetime.now(UTC)
    manual_end = now + timedelta(days=32)
    manual = [(
        SimpleNamespace(valid_from=now + timedelta(days=2), valid_until=manual_end),
        SimpleNamespace(approved_at=now - timedelta(minutes=1)),
    )]
    assert manual_anchor_end(manual, now) == manual_end
    assert historical_stars_start(now, manual, []) == manual_end
    assert manual_anchor_end(manual, now - timedelta(minutes=2)) is None


async def test_historical_refund_anchor_survives_late_unrelated_charge():
    """B prepaid while A was still paid. A's *later* refund must not
    shorten B when an older unrelated charge C is recovered afterward.
    """
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    buyer = _tid()
    try:
        owner, _, project_id, bot_uuid, _ = await _project(factory, _tid())
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id),
        ) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
            resource_id = await session.scalar(select(Resource.id).where(
                Resource.project_id == project_id,
            ))
            plan = Plan(
                project_id=project_id, name="Keep historical refund anchor",
                status="active", duration_value=1, duration_unit="day",
            )
            session.add(plan)
            await session.flush()
            session.add(PlanResource(
                project_id=project_id, plan_id=plan.id, resource_id=resource_id,
            ))
            price = await replace_active_plan_price(
                session, project_id=project_id, plan_id=plan.id,
                currency_code="XTR", amount="70",
            )
            price_id = price.id
        receipts = []
        for _ in range(3):
            async with system_project_session_scope(factory, project_id) as session:
                order = await create_stars_subscription_order(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer, username=None, first_name="Buyer",
                    plan_price_id=price_id,
                )
                receipts.append((order.id, order.invoice_payload))
        # 0=C (old), 1=A, 2=B. B was approved before A was refunded.
        paid_a = (datetime.now(UTC) - timedelta(minutes=5)).replace(microsecond=0)
        paid_b = paid_a + timedelta(minutes=1)
        refunded_a = paid_a + timedelta(minutes=2)
        paid_c = paid_a - timedelta(days=3)
        for index, paid_at in ((1, paid_a), (2, paid_b)):
            async with system_project_session_scope(factory, project_id) as session:
                await confirm_subscription_stars_payment(
                    session, project_id=project_id, bot_id=bot_id,
                    buyer_id=buyer, payload=receipts[index][1],
                    currency="XTR", amount=70, charge_id=f"history-{index}",
                    paid_at=paid_at,
                )
        expected_b_start = paid_a + timedelta(days=1)
        async with system_project_session_scope(factory, project_id) as session:
            before = await session.scalar(select(EntitlementGrant.valid_from).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == receipts[2][0],
            ))
            assert before == expected_b_start
        async with system_project_session_scope(factory, project_id) as session:
            await record_verified_subscription_star_refund(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer, payload=receipts[1][1], amount=70,
                currency="XTR", charge_id="history-1", refunded_at=refunded_a,
            )
        # C's old charge arrives from Telegram only after A was refunded.
        async with system_project_session_scope(factory, project_id) as session:
            await confirm_subscription_stars_payment(
                session, project_id=project_id, bot_id=bot_id,
                buyer_id=buyer, payload=receipts[0][1], amount=70,
                currency="XTR", charge_id="history-0", paid_at=paid_c,
            )
        async with system_project_session_scope(factory, project_id) as session:
            later = await session.scalar(select(EntitlementGrant).where(
                EntitlementGrant.project_id == project_id,
                EntitlementGrant.source_id == receipts[2][0],
            ))
            sub = await session.scalar(select(Subscription).where(
                Subscription.project_id == project_id, Subscription.plan_id == plan.id,
            ))
            assert later.status == "active"
            assert later.valid_from == expected_b_start
            assert later.valid_until == expected_b_start + timedelta(days=1)
            assert sub.last_star_order_id == receipts[2][0]
            assert sub.current_period_end == later.valid_until
            assert sub.status == "active"
    finally:
        await engine.dispose()
