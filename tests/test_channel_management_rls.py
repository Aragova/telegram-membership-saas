"""Channel removal keeps pending invoices and blocks real paid purchases."""
import os
from decimal import Decimal
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.channel_management import (
    read_channel_insights,
    remove_channel_without_paid_purchases,
)
from telegram_membership_saas.modules.owner_setup.plan_management import (
    delete_unused_owner_plan,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    edit_owner_draft_resource,
    ensure_telegram_actor,
    publish_owner_plan,
    save_owner_price,
    setup_overview,
)
from telegram_membership_saas.modules.owner_setup.tenant_admin import get_owner_plan_info
from telegram_membership_saas.modules.payments import PaymentRequest
from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.telegram_commerce import (
    create_checkout_for_telegram_user,
    list_checkout_options,
    review_payment_request_from_telegram,
    submit_checkout_for_telegram_user,
)
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)


def _tg():
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def _new_paid_channel(factory):
    owner_tg = _tg()
    actor = await ensure_telegram_actor(
        factory, telegram_id=owner_tg,
        username="channel_owner", first_name="Owner",
    )
    project = await create_owner_project(
        factory, actor_id=actor, name="Channel removal test",
    )
    resource = uuid4()
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor, project_id=project),
    ) as session:
        session.add(Resource(
            id=resource, project_id=project,
            resource_type=ResourceType.TELEGRAM_CHANNEL.value,
            name="My <private> channel",
            external_id="-100" + str(_tg()),
        ))
    plan = await create_owner_plan(
        factory, actor_id=actor, project_id=project,
        name="Monthly", duration_value=1, duration_unit="month",
        resource_id=resource,
    )
    await save_owner_price(
        factory, actor_id=actor, project_id=project,
        plan_id=plan, currency_code="RUB", amount="550",
    )
    await create_owner_payment_method(
        factory, actor_id=actor, project_id=project,
        name="Bank", currency_code="RUB",
        instruction="Pay {amount} {currency}",
    )
    await publish_owner_plan(
        factory, actor_id=actor, project_id=project, plan_id=plan,
    )
    buyer_tg = _tg()
    async with system_project_session_scope(factory, project) as session:
        offer = (await list_checkout_options(session, project_id=project))[0]
        checkout = await create_checkout_for_telegram_user(
            session, project_id=project,
            telegram_user_id=buyer_tg, username="buyer",
            first_name="Buyer", plan_price_id=offer.plan_price_id,
            payment_method_id=offer.payment_method_id,
        )
        request_id = checkout.request.id
    return owner_tg, actor, project, resource, plan, request_id, buyer_tg


async def test_pending_invoice_does_not_block_channel_removal_and_can_be_reviewed():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner_tg, owner, project, resource, plan, request, buyer_tid = (
            await _new_paid_channel(factory)
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=_tg(), username="outsider", first_name="Other",
        )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await read_channel_insights(
                factory, actor_id=outsider, project_id=project,
                resource_id=resource,
            )
        before = await read_channel_insights(
            factory, actor_id=owner, project_id=project,
            resource_id=resource,
        )
        assert before.active_plans == 1
        assert before.pending_invoices == 1
        assert before.paid_orders == 0
        assert before.sales_by_currency == ()
        assert before.active_paid_customers == 0
        assert await remove_channel_without_paid_purchases(
            factory, actor_id=owner, project_id=project,
            resource_id=resource,
        ) == ("My <private> channel", 1)
        owner_home = await setup_overview(
            factory, actor_id=owner, project_id=project,
        )
        assert owner_home.resources == ()
        assert owner_home.plans == ()
        with pytest.raises(ValueError):
            await read_channel_insights(
                factory, actor_id=owner, project_id=project,
                resource_id=resource,
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            channel = await session.get(Resource, resource)
            old_plan = await session.get(Plan, plan)
            invoice = await session.get(PaymentRequest, request)
            assert channel.removed_at is not None
            assert channel.status == "active"  # old paid obligations still settle
            assert old_plan.removed_at is not None and old_plan.status == "archived"
            assert invoice is not None and invoice.status == "waiting_confirmation"
        async with system_project_session_scope(factory, project) as session:
            assert await list_checkout_options(session, project_id=project) == ()
        # A buyer who actually transferred funds before removal is not
        # abandoned. Their existing review request still settles into access.
        async with system_project_session_scope(factory, project) as session:
            await submit_checkout_for_telegram_user(
                session, project_id=project,
                request_id=request, telegram_user_id=buyer_tid,
            )
        async with system_project_session_scope(factory, project) as session:
            approved = await review_payment_request_from_telegram(
                session, project_id=project, request_id=request,
                actor_telegram_user_id=owner_tg, approve=True,
            )
            assert approved.changed
    finally:
        await engine.dispose()


async def test_confirmed_payment_blocks_channel_deletion_permanently():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner_tg, owner, project, resource, _, request, buyer_id = (
            await _new_paid_channel(factory)
        )
        async with system_project_session_scope(factory, project) as session:
            await submit_checkout_for_telegram_user(
                session, project_id=project,
                request_id=request, telegram_user_id=buyer_id,
            )
        async with system_project_session_scope(factory, project) as session:
            approval = await review_payment_request_from_telegram(
                session, project_id=project, request_id=request,
                actor_telegram_user_id=owner_tg, approve=True,
            )
            assert approval.changed
        metrics = await read_channel_insights(
            factory, actor_id=owner, project_id=project,
            resource_id=resource,
        )
        assert metrics.paid_orders == 1
        assert metrics.sales_by_currency == (("RUB", Decimal("550"), 1),)
        assert metrics.paid_customers == 1
        assert metrics.active_paid_customers == 1
        with pytest.raises(ValueError, match="подтверждённые покупки"):
            await remove_channel_without_paid_purchases(
                factory, actor_id=owner, project_id=project,
                resource_id=resource,
            )
        assert (await setup_overview(
            factory, actor_id=owner, project_id=project,
        )).resources
    finally:
        await engine.dispose()


async def test_unpaid_tariff_removal_is_invisible_but_preserves_invoice_snapshot():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        _, owner, project, _, plan, request, _ = (
            await _new_paid_channel(factory)
        )
        info = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=plan,
        )
        assert info.has_history and not info.has_purchase
        await delete_unused_owner_plan(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, expected_status="active",
            expected_revision=info.revision,
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            old_plan = await session.get(Plan, plan)
            old_invoice = await session.get(PaymentRequest, request)
            assert old_plan is not None
            assert old_plan.removed_at is not None and old_plan.status == "archived"
            assert old_invoice is not None
        with pytest.raises(ValueError, match="Тариф не найден"):
            await get_owner_plan_info(
                factory, actor_id=owner, project_id=project, plan_id=plan,
            )
        async with system_project_session_scope(factory, project) as session:
            assert await list_checkout_options(session, project_id=project) == ()
    finally:
        await engine.dispose()


async def test_tombstoned_channel_cannot_be_resold_with_bulk_or_stale_draft():
    from telegram_membership_saas.modules.owner_setup.bulk_sales import (
        read_bulk_sales_state,
        switch_all_sales,
    )

    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        _, owner, project, resource_id, plan_id, _, _ = (
            await _new_paid_channel(factory)
        )
        old_bulk = await read_bulk_sales_state(
            factory, actor_id=owner, project_id=project, kind="plan",
        )
        assert old_bulk.active == 1
        await remove_channel_without_paid_purchases(
            factory, actor_id=owner, project_id=project, resource_id=resource_id,
        )
        bulk = await read_bulk_sales_state(
            factory, actor_id=owner, project_id=project, kind="plan",
        )
        assert bulk.active == bulk.paused == 0
        with pytest.raises(ValueError):
            await switch_all_sales(
                factory, actor_id=owner, project_id=project,
                kind="plan", enable=False, expected_revision=old_bulk.revision,
            )
        # Simulate a legacy/raw status mutation. Even an 'active' tombstone
        # must never reach a buyer checkout catalog.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            old = await session.get(Plan, plan_id)
            old.status = "active"
        async with system_project_session_scope(factory, project) as session:
            assert await list_checkout_options(session, project_id=project) == ()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            old = await session.get(Plan, plan_id)
            old.status = "archived"

        other = uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(Resource(
                id=other, project_id=project,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id="-100" + str(_tg()), name="Another channel",
            ))
        draft = await create_owner_plan(
            factory, actor_id=owner, project_id=project,
            resource_id=other, duration_value=1, duration_unit="month",
            name="New draft",
        )
        with pytest.raises(ValueError, match="действующий канал"):
            await edit_owner_draft_resource(
                factory, actor_id=owner, project_id=project,
                plan_id=draft, resource_id=resource_id,
            )
    finally:
        await engine.dispose()
