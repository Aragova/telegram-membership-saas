"""Read-only buyer invoice history uses existing ledger and forced project RLS."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import build_engine, build_session_factory, tenant_session_scope
from telegram_membership_saas.modules.owner_setup.subscribers import (
    list_subscriber_invoices,
    subscriber_detail,
)
from telegram_membership_saas.modules.payments import (
    PaymentRequest,
    create_manual_payment_method,
    create_payment_request,
    mark_payment_request_submitted,
    reject_payment_request,
)
from telegram_membership_saas.modules.plans import DurationUnit, Plan, PlanStatus
from telegram_membership_saas.modules.pricing import replace_active_plan_price
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext
from test_owner_subscribers_rls import setup_subscriber

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires real migrated PostgreSQL",
)


async def test_buyer_invoice_history_statuses_pagination_and_tenant_isolation():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, other, cid, _, _ = (
            await setup_subscriber(factory)
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            plan = Plan(
                project_id=project, name="Private monthly",
                duration_unit=DurationUnit.MONTH.value, duration_value=1,
                status=PlanStatus.ACTIVE.value,
            )
            session.add(plan)
            await session.flush()
            price = await replace_active_plan_price(
                session, project_id=project, plan_id=plan.id,
                currency_code="RUB", amount="500",
            )
            method = await create_manual_payment_method(
                session, project_id=project,
                name="Bank transfer", currency_codes=["RUB"],
                instruction_template="Transfer {amount} {currency} for {plan}",
            )
            ids = []
            for _ in range(9):
                invoice = await create_payment_request(
                    session, project_id=project, customer_id=cid,
                    plan_id=plan.id, plan_price_id=price.id,
                    payment_method_id=method.id,
                )
                ids.append(invoice.id)
            await mark_payment_request_submitted(
                session, project_id=project, request_id=ids[0],
            )
            await reject_payment_request(
                session, project_id=project,
                request_id=ids[0], reviewer_user_id=owner,
            )
        first = await list_subscriber_invoices(
            factory, actor_id=owner, project_id=project, customer_id=cid,
        )
        second = await list_subscriber_invoices(
            factory, actor_id=owner, project_id=project,
            customer_id=cid, page=1,
        )
        assert first.total == second.total == 9
        assert len(first.invoices) == 8 and len(second.invoices) == 1
        items = first.invoices + second.invoices
        assert {x.id for x in items} == set(ids)
        assert {x.status for x in items} == {"waiting_confirmation", "rejected"}
        assert all(x.plan_name == "Private monthly" for x in items)
        assert all(x.currency == "RUB" and x.amount.startswith("500") for x in items)
        info = await subscriber_detail(
            factory, actor_id=owner, project_id=project, customer_id=cid,
        )
        assert info.total_invoices == 9 and info.pending_invoices == 8

        # Neither another owner nor a different project can open these invoices.
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await list_subscriber_invoices(
                factory, actor_id=outsider, project_id=project,
                customer_id=cid,
            )
        with pytest.raises(ValueError, match="Подписчик"):
            await list_subscriber_invoices(
                factory, actor_id=outsider, project_id=other,
                customer_id=cid,
            )
        with pytest.raises(ValueError, match="Подписчик"):
            await list_subscriber_invoices(
                factory, actor_id=owner, project_id=project,
                customer_id=uuid4(),
            )
        with pytest.raises(ValueError, match="страница"):
            await list_subscriber_invoices(
                factory, actor_id=owner, project_id=project,
                customer_id=cid, page=-1,
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert await session.scalar(
                select(func.count(PaymentRequest.id)).where(
                    PaymentRequest.customer_id == cid,
                )
            ) == 9
    finally:
        await engine.dispose()
