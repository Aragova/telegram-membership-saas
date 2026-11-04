"""Real PostgreSQL tenant controls for prices and payment method currencies."""
import os
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    ensure_telegram_actor,
    save_owner_price,
)
from telegram_membership_saas.modules.owner_setup.tenant_admin import (
    attach_owner_method_currency,
    change_owner_method_status,
    get_owner_plan_info,
    list_owner_methods,
)
from telegram_membership_saas.modules.resources import (
    Resource,
    ResourceStatus,
    ResourceType,
)
from telegram_membership_saas.tenant_context import (
    TenantAccessDenied,
    TenantContext,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="Requires PostgreSQL tenant RLS",
)


def _telegram_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_owner_links_multiple_currencies_and_cross_tenant_is_denied():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_telegram_id(),
            username="methods_owner", first_name="Owner",
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=_telegram_id(),
            username="methods_outsider", first_name="Outsider",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Payments owner",
        )
        other = await create_owner_project(
            factory, actor_id=outsider, name="Other owner",
        )
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=project,
            name="Bank transfer", currency_code="RUB",
            instruction="Переведите {amount} {currency}.",
        )
        methods = await list_owner_methods(
            factory, actor_id=owner, project_id=project,
        )
        assert len(methods) == 1
        method = methods[0]
        assert method.currencies == ("RUB",)
        assert await attach_owner_method_currency(
            factory, actor_id=owner, project_id=project,
            method_id=method.id, currency_code="USD",
        )
        assert not await attach_owner_method_currency(
            factory, actor_id=owner, project_id=project,
            method_id=method.id, currency_code="USD",
        )
        currencies = (await list_owner_methods(
            factory, actor_id=owner, project_id=project,
        ))[0].currencies
        assert currencies == ("RUB", "USD")
        with pytest.raises((TenantAccessDenied, PermissionError, ValueError)):
            await attach_owner_method_currency(
                factory, actor_id=outsider, project_id=other,
                method_id=method.id, currency_code="EUR",
            )
        with pytest.raises((TenantAccessDenied, PermissionError)):
            await change_owner_method_status(
                factory, actor_id=outsider, project_id=project,
                method_id=method.id, enabled=False,
            )

        resource_id = uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(Resource(
                id=resource_id, project_id=project,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=str(-_telegram_id()),
                name="VIP test channel", status=ResourceStatus.ACTIVE.value,
            ))
        plan = await create_owner_plan(
            factory, actor_id=owner, project_id=project,
            name="VIP test plan", duration_value=1,
            duration_unit="month", resource_id=resource_id,
        )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, currency_code="RUB", amount="10",
        )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, currency_code="USD", amount="7",
        )
        detail = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project,
            plan_id=plan,
        )
        assert detail.resource_name == "VIP test channel"
        assert {p.currency: p.method_count for p in detail.prices} == {
            "RUB": 1, "USD": 1,
        }
        assert await change_owner_method_status(
            factory, actor_id=owner, project_id=project,
            method_id=method.id, enabled=False,
        )
        detail = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project,
            plan_id=plan,
        )
        assert {p.currency: p.method_count for p in detail.prices} == {
            "RUB": 0, "USD": 0,
        }
        with pytest.raises((TenantAccessDenied, PermissionError, ValueError)):
            await get_owner_plan_info(
                factory, actor_id=outsider, project_id=other,
                plan_id=plan,
            )
    finally:
        await engine.dispose()
