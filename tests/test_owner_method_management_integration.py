"""Real PostgreSQL RLS checks for owner payment method lifecycle."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine, build_session_factory, system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.owner_setup.method_management import (
    change_owner_method_status_guarded,
    delete_unused_owner_method,
    detach_owner_method_currency,
    edit_owner_method_instructions,
    rename_owner_method,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method, create_owner_plan, create_owner_project,
    ensure_telegram_actor, publish_owner_plan, save_owner_price,
)
from telegram_membership_saas.modules.owner_setup.tenant_admin import (
    attach_owner_method_currency, get_owner_method_info, list_owner_methods,
)
from telegram_membership_saas.modules.payments import PaymentMethod
from telegram_membership_saas.modules.resources import Resource, ResourceType
from telegram_membership_saas.modules.telegram_commerce import (
    create_checkout_for_telegram_user, list_checkout_options,
)
from telegram_membership_saas.tenant_context import (
    TenantAccessDenied, TenantContext,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL RLS",
)


def _tid():
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_owner_method_edit_currency_disable_and_historical_delete_guard():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username="method_owner",
            first_name="Owner",
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=_tid(), username="method_outsider",
            first_name="Outsider",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Method lifecycle",
        )
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=project, name="First bank",
            currency_code="RUB", instruction="Pay {amount} {currency}",
        )
        method = (await list_owner_methods(
            factory, actor_id=owner, project_id=project,
        ))[0]
        method_id = method.id
        assert await attach_owner_method_currency(
            factory, actor_id=owner, project_id=project,
            method_id=method_id, currency_code="USD",
        )
        previous = await get_owner_method_info(
            factory, actor_id=owner, project_id=project,
            method_id=method_id,
        )
        assert previous.currencies == ("RUB", "USD")
        assert not previous.has_history

        with pytest.raises((PermissionError, TenantAccessDenied)):
            await rename_owner_method(
                factory, actor_id=outsider, project_id=project,
                method_id=method_id, name="Stolen",
            )
        assert await rename_owner_method(
            factory, actor_id=owner, project_id=project,
            method_id=method_id, name=" Updated bank ",
        ) == "Updated bank"
        with pytest.raises(ValueError, match="изменился"):
            await delete_unused_owner_method(
                factory, actor_id=owner, project_id=project,
                method_id=method_id, expected_revision=previous.revision,
            )

        with pytest.raises(ValueError, match="Unsupported"):
            await edit_owner_method_instructions(
                factory, actor_id=owner, project_id=project,
                method_id=method_id, instruction="Pay {secret}",
            )
        await edit_owner_method_instructions(
            factory, actor_id=owner, project_id=project,
            method_id=method_id, instruction="Send {amount} {currency} to bank",
        )
        current = await get_owner_method_info(
            factory, actor_id=owner, project_id=project,
            method_id=method_id,
        )
        assert current.instruction == "Send {amount} {currency} to bank"
        await detach_owner_method_currency(
            factory, actor_id=owner, project_id=project,
            method_id=method_id, currency_code="USD",
            expected_revision=current.revision,
        )
        current = await get_owner_method_info(
            factory, actor_id=owner, project_id=project,
            method_id=method_id,
        )
        assert current.currencies == ("RUB",)
        with pytest.raises(ValueError, match="последнюю"):
            await detach_owner_method_currency(
                factory, actor_id=owner, project_id=project,
                method_id=method_id, currency_code="RUB",
                expected_revision=current.revision,
            )

        await change_owner_method_status_guarded(
            factory, actor_id=owner, project_id=project,
            method_id=method_id, enabled=False,
            expected_revision=current.revision,
        )
        with pytest.raises(ValueError, match="изменился"):
            await change_owner_method_status_guarded(
                factory, actor_id=owner, project_id=project,
                method_id=method_id, enabled=True,
                expected_revision=current.revision,
            )
        disabled = await get_owner_method_info(
            factory, actor_id=owner, project_id=project,
            method_id=method_id,
        )
        assert disabled.status == "disabled"
        await change_owner_method_status_guarded(
            factory, actor_id=owner, project_id=project,
            method_id=method_id, enabled=True,
            expected_revision=disabled.revision,
        )

        resource_id = uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(Resource(
                id=resource_id, project_id=project,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=f"-100{uuid4().int % 10_000_000_000}",
                name="Method channel",
            ))
        plan_id = await create_owner_plan(
            factory, actor_id=owner, project_id=project,
            name="Monthly method", duration_value=1,
            duration_unit="month", resource_id=resource_id,
        )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan_id, currency_code="RUB", amount="120",
        )
        await publish_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=plan_id,
        )
        async with system_project_session_scope(factory, project) as session:
            offer = (await list_checkout_options(
                session, project_id=project,
            ))[0]
            await create_checkout_for_telegram_user(
                session, project_id=project,
                telegram_user_id=_tid(), username="buyer",
                first_name="Buyer", plan_price_id=offer.plan_price_id,
                payment_method_id=method_id,
            )
        sold = await get_owner_method_info(
            factory, actor_id=owner, project_id=project,
            method_id=method_id,
        )
        assert sold.has_history
        with pytest.raises(ValueError, match="история"):
            await delete_unused_owner_method(
                factory, actor_id=owner, project_id=project,
                method_id=method_id, expected_revision=sold.revision,
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert await session.scalar(
                select(PaymentMethod.id).where(PaymentMethod.id == method_id)
            )
        # New unused method can be permanently removed.
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=project, name="Throwaway",
            currency_code="EUR", instruction="Transfer {amount} {currency}",
        )
        unused = next(m for m in await list_owner_methods(
            factory, actor_id=owner, project_id=project,
        ) if m.name == "Throwaway")
        unused_info = await get_owner_method_info(
            factory, actor_id=owner, project_id=project,
            method_id=unused.id,
        )
        await delete_unused_owner_method(
            factory, actor_id=owner, project_id=project,
            method_id=unused.id, expected_revision=unused_info.revision,
        )
        assert all(m.id != unused.id for m in await list_owner_methods(
            factory, actor_id=owner, project_id=project,
        ))
    finally:
        await engine.dispose()
