"""Stars plans: real PostgreSQL payment -> subscription -> access outbox.

Every buyer action is under a real project scope. Same charge retries grant
exactly once; wrong buyer/amount/bot cannot deliver. Manual settlement remains.
"""
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.entitlements import EntitlementGrant
from telegram_membership_saas.modules.bots.deletion_service import (
    count_retirement_blockers,
)
from telegram_membership_saas.modules.stars_subscription_refunds import (
    record_verified_subscription_star_refund,
)
from telegram_membership_saas.modules.stars_subscriptions import (
    SubscriptionStarOrder,
    SubscriptionStarRefund,
)
from telegram_membership_saas.modules.outbox import OutboxEvent
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import replace_active_plan_price
from telegram_membership_saas.modules.resources import Resource
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
