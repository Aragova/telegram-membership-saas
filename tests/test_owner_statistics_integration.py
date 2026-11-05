"""Owner statistics are RLS-scoped and based on real approved payments."""
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    ensure_telegram_actor,
    publish_owner_plan,
    save_owner_price,
)
from telegram_membership_saas.modules.owner_setup.statistics import read_owner_statistics
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.subscriptions import Subscription
from telegram_membership_saas.modules.telegram_commerce import (
    create_checkout_for_telegram_user,
    list_checkout_options,
    review_payment_request_from_telegram,
    submit_checkout_for_telegram_user,
)
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL tenant RLS",
)


def _tid():
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_owner_stats_confirmed_sales_and_other_tenant_denial():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner_tid, buyer_tid = _tid(), _tid()
        owner = await ensure_telegram_actor(
            factory, telegram_id=owner_tid, username="stats_owner",
            first_name="Owner",
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username="stats_other",
            first_name="Other",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Statistics owner",
        )
        other_project = await create_owner_project(
            factory, actor_id=outsider, name="Statistics other",
        )
        resource_id = uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(Resource(
                id=resource_id, project_id=project,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="Statistics test channel",
            ))
        plan = await create_owner_plan(
            factory, actor_id=owner, project_id=project,
            name="Stats monthly", duration_value=1,
            duration_unit="month", resource_id=resource_id,
        )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, currency_code="RUB", amount="550.25",
        )
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=project,
            name="Bank", currency_code="RUB",
            instruction="Pay {amount} {currency}",
        )
        await publish_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=plan,
        )
        empty = await read_owner_statistics(
            factory, actor_id=owner, project_id=project, period="all",
        )
        assert empty.payment_count == 0
        assert empty.active_paid_customers == 0
        assert empty.currency_totals == ()
        assert (await read_owner_statistics(
            factory, actor_id=outsider, project_id=other_project, period="all",
        )).payment_count == 0
        with pytest.raises((TenantAccessDenied, PermissionError)):
            await read_owner_statistics(
                factory, actor_id=outsider, project_id=project, period="all",
            )

        async with system_project_session_scope(factory, project) as session:
            option = (await list_checkout_options(session, project_id=project))[0]
            checkout = await create_checkout_for_telegram_user(
                session, project_id=project,
                telegram_user_id=buyer_tid, username="stats_buyer",
                first_name="Buyer", plan_price_id=option.plan_price_id,
                payment_method_id=option.payment_method_id,
            )
            request_id = checkout.request.id
        before = await read_owner_statistics(
            factory, actor_id=owner, project_id=project, period="all",
        )
        assert before.payment_count == 0  # A checkout request is not a sale.
        assert before.total_customers == 1
        async with system_project_session_scope(factory, project) as session:
            await submit_checkout_for_telegram_user(
                session, project_id=project,
                request_id=request_id, telegram_user_id=buyer_tid,
            )
        awaiting = await read_owner_statistics(
            factory, actor_id=owner, project_id=project, period="all",
        )
        assert awaiting.waiting_review == 1
        assert awaiting.payment_count == 0
        async with system_project_session_scope(factory, project) as session:
            approved = await review_payment_request_from_telegram(
                session, project_id=project, request_id=request_id,
                actor_telegram_user_id=owner_tid, approve=True,
            )
            assert approved.changed
        final = await read_owner_statistics(
            factory, actor_id=owner, project_id=project, period="all",
        )
        assert final.payment_count == 1
        assert final.currency_totals == (("RUB", Decimal("550.25"), 1),)
        assert final.active_paid_customers == 1
        assert final.active_manual_grants == 0
        assert final.waiting_review == 0
        # Real renewal keeps started_at, but moves current_period_start forward.
        # An owner must still see that customer as an active paying subscriber.
        moment = datetime.now(UTC)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            subscription = await session.scalar(
                select(Subscription).where(Subscription.project_id == project)
            )
            assert subscription is not None
            assert subscription.started_at <= moment
            subscription.current_period_start = moment + timedelta(days=5)
        renewed = await read_owner_statistics(
            factory, actor_id=owner, project_id=project, period="all", now=moment,
        )
        assert renewed.active_paid_customers == 1
        assert (await read_owner_statistics(
            factory, actor_id=outsider, project_id=other_project, period="all",
        )).currency_totals == ()
    finally:
        await engine.dispose()
