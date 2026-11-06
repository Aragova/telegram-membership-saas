"""Real PostgreSQL: monetary snapshots survive project/bot lifecycle attacks."""
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_project,
    ensure_telegram_actor,
)
from telegram_membership_saas.modules.payments import (
    Payment,
    PaymentRequest,
    approve_payment_request,
    create_manual_payment_method,
    create_payment_request,
    get_or_create_project_customer,
    mark_payment_request_submitted,
    reject_payment_request,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import replace_active_plan_price
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="Requires real PostgreSQL and applied Alembic migrations",
)


def _id() -> int:
    return uuid4().int % 7_000_000_000_000_000_000 + 1


async def _checkout(session, *, project, buyer):
    resource = Resource(
        project_id=project, resource_type="telegram_channel",
        external_id="-100" + str(uuid4().int % 10**12).zfill(12),
        name="Financial guard test",
    )
    plan = Plan(
        project_id=project, name="Month",
        duration_value=1, duration_unit="month", status="active",
    )
    session.add_all([resource, plan])
    await session.flush()
    session.add(PlanResource(
        project_id=project, plan_id=plan.id, resource_id=resource.id,
    ))
    price = await replace_active_plan_price(
        session, project_id=project, plan_id=plan.id,
        currency_code="USD", amount="10",
    )
    method = await create_manual_payment_method(
        session, project_id=project, name="Bank USD",
        currency_codes=["USD"], instruction_template="Pay {amount} USD",
    )
    customer = await get_or_create_project_customer(
        session, project_id=project, user_id=buyer,
    )
    return await create_payment_request(
        session, project_id=project, customer_id=customer.id,
        plan_id=plan.id, plan_price_id=price.id,
        payment_method_id=method.id,
    )


async def _blocked(session, sql, params, expected):
    with pytest.raises(DBAPIError) as error:
        async with session.begin_nested():
            await session.execute(text(sql), params)
    assert getattr(error.value.orig, "sqlstate", None) == expected


async def test_approved_payment_and_checkout_price_are_immutable():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_id(), username=None, first_name=None,
        )
        buyer = await ensure_telegram_actor(
            factory, telegram_id=_id(), username=None, first_name=None,
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Immutable payment business",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            request = await _checkout(session, project=project, buyer=buyer)
            request_id = request.id
            # Normal checkout transition to review and normal one-time
            # approval must still succeed under the new DB triggers.
            await mark_payment_request_submitted(
                session, project_id=project, request_id=request_id,
            )
            result = await approve_payment_request(
                session, project_id=project, request_id=request_id,
                reviewer_user_id=owner,
            )
            repeated = await approve_payment_request(
                session, project_id=project, request_id=request_id,
                reviewer_user_id=owner,
            )
            assert repeated.payment.id == result.payment.id
            payment_id = result.payment.id
            assert result.payment.final_amount == Decimal("10")

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            for column, value in [
                ("final_amount", "1"),
                ("currency_code", "RUB"),
                ("approved_at", datetime(2100, 1, 1, tzinfo=UTC)),
            ]:
                await _blocked(
                    session,
                    f"UPDATE payments SET {column}=:value WHERE id=:id",
                    {"value": value, "id": payment_id}, "P6901",
                )
            await _blocked(
                session, "DELETE FROM payments WHERE id=:id",
                {"id": payment_id}, "P6901",
            )
            await _blocked(
                session,
                "UPDATE payment_requests SET final_amount=1 WHERE id=:id",
                {"id": request_id}, "P6903",
            )
            await _blocked(
                session,
                "UPDATE payment_requests SET status='rejected' WHERE id=:id",
                {"id": request_id}, "P6904",
            )
            await _blocked(
                session, "DELETE FROM payment_requests WHERE id=:id",
                {"id": request_id}, "P6902",
            )
            paid = await session.scalar(select(Payment).where(
                Payment.id == payment_id,
            ))
            frozen = await session.scalar(select(PaymentRequest).where(
                PaymentRequest.id == request_id,
            ))
            assert paid.final_amount == Decimal("10")
            assert paid.currency_code == "USD"
            assert frozen.status == "approved"
            assert frozen.final_amount == Decimal("10")

        # Disconnected projects with real payment history cannot be purged
        # through the project's cascades, even by bypassing the app's UI.
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.delete(await session.get(Project, project))
                await session.flush()
    finally:
        await engine.dispose()


async def test_unapproved_checkout_is_mutable_only_in_state_not_price():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_id(), username=None, first_name=None,
        )
        buyer = await ensure_telegram_actor(
            factory, telegram_id=_id(), username=None, first_name=None,
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Frozen request snapshots",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            request = await _checkout(session, project=project, buyer=buyer)
            rid = request.id
            assert request.status == "waiting_confirmation"
            await _blocked(
                session,
                "UPDATE payment_requests SET currency_code='EUR' WHERE id=:id",
                {"id": rid}, "P6903",
            )
            await _blocked(
                session,
                "UPDATE payment_requests SET discount_amount=5, final_amount=5 "
                "WHERE id=:id", {"id": rid}, "P6903",
            )
            submitted = await mark_payment_request_submitted(
                session, project_id=project, request_id=rid,
            )
            assert submitted.status == "waiting_review"
            rejected = await reject_payment_request(
                session, project_id=project, request_id=rid,
                reviewer_user_id=owner,
            )
            assert rejected.status == "rejected"
            await _blocked(
                session,
                "UPDATE payment_requests SET status='waiting_review' WHERE id=:id",
                {"id": rid}, "P6904",
            )
            await _blocked(
                session, "DELETE FROM payment_requests WHERE id=:id",
                {"id": rid}, "P6902",
            )
            stored = await session.scalar(select(PaymentRequest).where(
                PaymentRequest.id == rid,
            ))
            assert stored.status == "rejected"
            assert stored.final_amount == Decimal("10")
    finally:
        await engine.dispose()
