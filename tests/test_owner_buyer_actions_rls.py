"""Real PostgreSQL tests for customer moderation and free plan activation."""
import os

import pytest
from sqlalchemy import func, select
from test_owner_subscribers_rls import setup_subscriber

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.entitlements import (
    EntitlementGrant,
    EntitlementGrantStatus,
    EntitlementSource,
)
from telegram_membership_saas.modules.owner_setup.subscribers import (
    activate_manual_plan,
    revoke_manual_plan,
    set_customer_ban,
    subscriber_detail,
)
from telegram_membership_saas.modules.payments import (
    Payment,
    PaymentRequest,
    create_manual_payment_method,
)
from telegram_membership_saas.modules.plans import (
    DurationUnit,
    Plan,
    PlanResource,
    PlanStatus,
)
from telegram_membership_saas.modules.pricing import replace_active_plan_price
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_commerce.service import (
    create_checkout_for_telegram_user,
)
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext
pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires migrated PostgreSQL",
)


async def test_banned_buyer_has_no_new_checkout_and_history_is_preserved():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, other, cid, rid, buyer_tid = (
            await setup_subscriber(factory)
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            plan = Plan(
                project_id=project, name="Test month",
                duration_value=1, duration_unit=DurationUnit.MONTH.value,
                status=PlanStatus.ACTIVE.value,
            )
            session.add(plan)
            await session.flush()
            session.add(PlanResource(
                project_id=project, plan_id=plan.id, resource_id=rid,
            ))
            price = await replace_active_plan_price(
                session, project_id=project, plan_id=plan.id,
                currency_code="RUB", amount="500",
            )
            method = await create_manual_payment_method(
                session, project_id=project, name="Bank",
                currency_codes=["RUB"],
                instruction_template="Transfer {amount} {currency} for {plan}",
            )
            pid, price_id, method_id = plan.id, price.id, method.id

        with pytest.raises((PermissionError, TenantAccessDenied)):
            await set_customer_ban(
                factory, actor_id=outsider, project_id=project,
                customer_id=cid, banned=True,
            )
        with pytest.raises(ValueError, match="Подписчик"):
            await set_customer_ban(
                factory, actor_id=outsider, project_id=other,
                customer_id=cid, banned=True,
            )
        assert await set_customer_ban(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, banned=True,
        )
        assert not await set_customer_ban(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, banned=True,
        )
        detail = await subscriber_detail(
            factory, actor_id=owner, project_id=project, customer_id=cid,
        )
        assert detail.is_banned

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            with pytest.raises(PermissionError, match="заблокировал"):
                await create_checkout_for_telegram_user(
                    session, project_id=project,
                    telegram_user_id=buyer_tid, username="testbuyer",
                    first_name="Buyer",
                    plan_price_id=price_id, payment_method_id=method_id,
                )
            assert await session.scalar(
                select(func.count(PaymentRequest.id)).where(
                    PaymentRequest.project_id == project,
                    PaymentRequest.customer_id == cid,
                )
            ) == 0

        with pytest.raises(ValueError, match="блокировку"):
            await activate_manual_plan(
                factory, actor_id=owner, project_id=project,
                customer_id=cid, plan_id=pid,
            )
        assert await set_customer_ban(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, banned=False,
        )
        result = await activate_manual_plan(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, plan_id=pid,
        )
        assert result.changed and result.telegram_id == buyer_tid
        with pytest.raises(ValueError, match="уже выдан"):
            await activate_manual_plan(
                factory, actor_id=owner, project_id=project,
                customer_id=cid, plan_id=pid,
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            grants = (await session.scalars(
                select(EntitlementGrant).where(
                    EntitlementGrant.project_id == project,
                    EntitlementGrant.customer_id == cid,
                    EntitlementGrant.manual_plan_id == pid,
                )
            )).all()
            assert len(grants) == 1
            assert grants[0].source == EntitlementSource.MANUAL.value
            assert grants[0].status == EntitlementGrantStatus.ACTIVE.value
            assert grants[0].valid_until is not None
            assert await session.scalar(
                select(func.count(Payment.id)).where(Payment.project_id == project)
            ) == 0
            assert await session.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.project_id == project,
                )
            ) == 0
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await revoke_manual_plan(
                factory, actor_id=outsider, project_id=project,
                customer_id=cid, activation_id=result.grant_id,
            )
        revoke = await revoke_manual_plan(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, activation_id=result.grant_id,
        )
        assert revoke.changed
        assert not (await revoke_manual_plan(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, activation_id=result.grant_id,
        )).changed
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            revoked = await session.scalar(
                select(EntitlementGrant).where(
                    EntitlementGrant.source_id == result.grant_id,
                )
            )
            assert revoked.status == EntitlementGrantStatus.REVOKED.value
            assert await session.scalar(
                select(func.count(Payment.id)).where(Payment.project_id == project)
            ) == 0
    finally:
        await engine.dispose()
