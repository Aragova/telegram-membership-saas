"""PostgreSQL/RLS regression for compact bulk plan and payment controls."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.bulk_sales import (
    read_bulk_sales_state,
    switch_all_sales,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    ensure_telegram_actor,
    publish_owner_plan,
    save_owner_price,
)
from telegram_membership_saas.modules.payments import PaymentMethod, PaymentRequest
from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.telegram_commerce import (
    create_checkout_for_telegram_user,
    list_checkout_options,
)
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL tenant RLS",
)


def _tid() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_bulk_switches_preserve_history_and_restore_only_ready_plans():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username="bulk_owner", first_name="Owner",
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username="bulk_outsider", first_name="Other",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Bulk sales switches",
        )
        other = await create_owner_project(
            factory, actor_id=outsider, name="Independent tenant",
        )
        resource = uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(Resource(
                id=resource, project_id=project,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=str(-_tid()), name="Paid channel",
            ))
        for name in ("Bank A", "Bank B"):
            await create_owner_payment_method(
                factory, actor_id=owner, project_id=project,
                name=name, currency_code="RUB",
                instruction="Transfer {amount} {currency} to the bank",
            )
        plans = []
        for idx in range(3):
            pid = await create_owner_plan(
                factory, actor_id=owner, project_id=project,
                name=f"Plan {idx + 1}", duration_value=1,
                duration_unit="month", resource_id=resource,
            )
            plans.append(pid)
            if idx < 2:
                await save_owner_price(
                    factory, actor_id=owner, project_id=project,
                    plan_id=pid, currency_code="RUB", amount="350",
                )
                await publish_owner_plan(
                    factory, actor_id=owner, project_id=project, plan_id=pid,
                )
        initial = await read_bulk_sales_state(
            factory, actor_id=owner, project_id=project, kind="plan",
        )
        assert (initial.active, initial.paused, initial.drafts) == (2, 0, 1)
        assert initial.enable_action is False
        async with system_project_session_scope(factory, project) as session:
            offers = await list_checkout_options(session, project_id=project)
            assert offers
            request = await create_checkout_for_telegram_user(
                session, project_id=project,
                telegram_user_id=_tid(), username="bulk_buyer",
                first_name="Buyer", plan_price_id=offers[0].plan_price_id,
                payment_method_id=offers[0].payment_method_id,
            )
            original_request_id = request.request.id

        with pytest.raises((PermissionError, TenantAccessDenied)):
            await switch_all_sales(
                factory, actor_id=outsider, project_id=project,
                kind="plan", enable=False, expected_revision=initial.revision,
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await read_bulk_sales_state(
                factory, actor_id=owner, project_id=other, kind="method",
            )

        assert await switch_all_sales(
            factory, actor_id=owner, project_id=project,
            kind="plan", enable=False, expected_revision=initial.revision,
        ) == 2
        with pytest.raises(ValueError, match="изменились"):
            await switch_all_sales(
                factory, actor_id=owner, project_id=project,
                kind="plan", enable=False, expected_revision=initial.revision,
            )
        after_off = await read_bulk_sales_state(
            factory, actor_id=owner, project_id=project, kind="plan",
        )
        assert (after_off.active, after_off.paused, after_off.drafts) == (0, 2, 1)
        async with system_project_session_scope(factory, project) as session:
            assert not await list_checkout_options(session, project_id=project)
            assert await session.scalar(
                select(func.count(PaymentRequest.id)).where(
                    PaymentRequest.project_id == project,
                    PaymentRequest.id == original_request_id,
                )
            ) == 1

        methods = await read_bulk_sales_state(
            factory, actor_id=owner, project_id=project, kind="method",
        )
        assert (methods.active, methods.paused) == (2, 0)
        assert await switch_all_sales(
            factory, actor_id=owner, project_id=project,
            kind="method", enable=False, expected_revision=methods.revision,
        ) == 2
        # Restoring archived plans requires active payment methods; no partial
        # change is allowed when their prices cannot be paid.
        with pytest.raises(ValueError, match="действующая оплата"):
            await switch_all_sales(
                factory, actor_id=owner, project_id=project, kind="plan",
                enable=True, expected_revision=after_off.revision,
            )
        assert (await read_bulk_sales_state(
            factory, actor_id=owner, project_id=project, kind="plan",
        )).paused == 2
        disabled = await read_bulk_sales_state(
            factory, actor_id=owner, project_id=project, kind="method",
        )
        assert await switch_all_sales(
            factory, actor_id=owner, project_id=project,
            kind="method", enable=True, expected_revision=disabled.revision,
        ) == 2
        assert await switch_all_sales(
            factory, actor_id=owner, project_id=project,
            kind="plan", enable=True, expected_revision=after_off.revision,
        ) == 2
        restored = await read_bulk_sales_state(
            factory, actor_id=owner, project_id=project, kind="plan",
        )
        assert (restored.active, restored.paused, restored.drafts) == (2, 0, 1)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            statuses = (await session.execute(
                select(Plan.id, Plan.status).where(Plan.project_id == project)
            )).all()
            assert dict(statuses)[plans[2]] == "draft"
            assert len(statuses) == 3
            assert await session.scalar(
                select(func.count(PaymentMethod.id)).where(
                    PaymentMethod.project_id == project,
                    PaymentMethod.status == "active",
                )
            ) == 2
        async with system_project_session_scope(factory, project) as session:
            assert await list_checkout_options(session, project_id=project)
    finally:
        await engine.dispose()
