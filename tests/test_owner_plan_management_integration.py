"""PostgreSQL RLS contract for safe existing-plan lifecycle management."""
import os
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
from telegram_membership_saas.modules.owner_setup.plan_management import (
    archive_owner_plan,
    delete_unused_owner_plan,
    edit_owner_draft_duration,
    edit_owner_draft_resource,
    rename_owner_plan,
)
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    ensure_telegram_actor,
    publish_owner_plan,
    save_owner_price,
)
from telegram_membership_saas.modules.owner_setup.tenant_admin import (
    get_owner_plan_info,
)
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
    not os.getenv("TMS_DATABASE_URL"),
    reason="Requires PostgreSQL tenant RLS",
)


def _telegram_id():
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_existing_plan_edit_archive_restore_and_retain_sale_history():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner_tid = _telegram_id()
        owner = await ensure_telegram_actor(
            factory, telegram_id=owner_tid,
            username="plan_owner", first_name="Owner",
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=_telegram_id(),
            username="other_plan_owner", first_name="Other",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Editable plan test",
        )
        first_id, second_id = uuid4(), uuid4()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add_all([
                Resource(
                    id=first_id, project_id=project,
                    resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                    external_id=f"-100{uuid4().int % 10_000_000_000}",
                    name="First channel",
                ),
                Resource(
                    id=second_id, project_id=project,
                    resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                    external_id=f"-100{uuid4().int % 10_000_000_000}",
                    name="Second channel",
                ),
            ])

        plan = await create_owner_plan(
            factory, actor_id=owner, project_id=project,
            name="Monthly plan", duration_value=1, duration_unit="month",
            resource_id=first_id,
        )
        assert await rename_owner_plan(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, name="  Premium monthly  ",
        ) == "Premium monthly"
        unnamed = await create_owner_plan(
            factory, actor_id=owner, project_id=project,
            name="", duration_value=1, duration_unit="month",
            resource_id=first_id,
        )
        await edit_owner_draft_duration(
            factory, actor_id=owner, project_id=project,
            plan_id=unnamed, duration_value=3, duration_unit="month",
        )
        unnamed_info = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=unnamed,
        )
        assert unnamed_info.name == "3 месяца"
        await edit_owner_draft_duration(
            factory, actor_id=owner, project_id=project,
            plan_id=unnamed, duration_value=1, duration_unit="year",
        )
        unnamed_info = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=unnamed,
        )
        assert unnamed_info.name == "1 год"

        await edit_owner_draft_duration(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, duration_value=3, duration_unit="month",
        )
        await edit_owner_draft_resource(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, resource_id=second_id,
        )
        info = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=plan,
        )
        assert info.name == "Premium monthly"
        assert info.duration_value == 3
        assert info.resource_name == "Second channel"
        assert not info.has_history
        with pytest.raises((TenantAccessDenied, PermissionError)):
            await archive_owner_plan(
                factory, actor_id=outsider, project_id=project,
                plan_id=plan, expected_revision=info.revision,
            )
        with pytest.raises((TenantAccessDenied, PermissionError)):
            await rename_owner_plan(
                factory, actor_id=outsider, project_id=project,
                plan_id=plan, name="Hijacked",
            )

        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=plan, currency_code="RUB", amount="550",
        )
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=project,
            name="Bank", currency_code="RUB",
            instruction="Transfer {amount} {currency}",
        )
        await publish_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=plan,
        )
        with pytest.raises(ValueError, match="неопубликованного"):
            await edit_owner_draft_duration(
                factory, actor_id=owner, project_id=project,
                plan_id=plan, duration_value=1, duration_unit="week",
            )
        with pytest.raises(ValueError, match="неопубликованного"):
            await edit_owner_draft_resource(
                factory, actor_id=owner, project_id=project,
                plan_id=plan, resource_id=first_id,
            )

        async with system_project_session_scope(factory, project) as session:
            options = await list_checkout_options(session, project_id=project)
            assert len(options) == 1
            offer = options[0]
            checkout = await create_checkout_for_telegram_user(
                session, project_id=project,
                telegram_user_id=_telegram_id(), username="customer",
                first_name="Customer",
                plan_price_id=offer.plan_price_id,
                payment_method_id=offer.payment_method_id,
            )
            request_id = checkout.request.id
            customer_tid = checkout.request.customer_id

        previous = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=plan,
        )
        await archive_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=plan,
            expected_revision=previous.revision,
        )
        # A legacy publication button must not revive archived sales.
        with pytest.raises(ValueError, match="изменился"):
            await publish_owner_plan(
                factory, actor_id=owner, project_id=project, plan_id=plan,
            )
        async with system_project_session_scope(factory, project) as session:
            assert not await list_checkout_options(session, project_id=project)
        info = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=plan,
        )
        assert info.status == "archived"
        assert info.has_history
        with pytest.raises(ValueError, match="историю"):
            await delete_unused_owner_plan(
                factory, actor_id=owner, project_id=project,
                plan_id=plan, expected_status="archived",
                expected_revision=info.revision,
            )
        with pytest.raises(ValueError, match="изменился"):
            await delete_unused_owner_plan(
                factory, actor_id=owner, project_id=project,
                plan_id=plan, expected_status="active",
                expected_revision=info.revision,
            )

        # Existing obligation can still finish despite the plan being archived.
        async with system_project_session_scope(factory, project) as session:
            from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
            # Find the actual Telegram buyer ID via project-scoped user reference.
            actual_tid = await session.scalar(
                select(TelegramUser.telegram_user_id)
                .join(ProjectCustomer, ProjectCustomer.user_id == TelegramUser.id)
                .where(ProjectCustomer.id == customer_tid)
            )
            assert actual_tid
            await submit_checkout_for_telegram_user(
                session, project_id=project,
                request_id=request_id, telegram_user_id=actual_tid,
            )
        async with system_project_session_scope(factory, project) as session:
            approved = await review_payment_request_from_telegram(
                session, project_id=project, request_id=request_id,
                actor_telegram_user_id=owner_tid,
                approve=True,
            )
            assert approved.changed
        async with system_project_session_scope(factory, project) as session:
            assert await session.scalar(select(PaymentRequest.id).where(
                PaymentRequest.id == request_id,
                PaymentRequest.project_id == project,
            ))
        await publish_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=plan,
            expected_status="archived",
            expected_revision=info.revision,
        )
        async with system_project_session_scope(factory, project) as session:
            assert len(await list_checkout_options(session, project_id=project)) == 1

        # An unsold plan can be permanently deleted with its prices/links.
        unused = await create_owner_plan(
            factory, actor_id=owner, project_id=project,
            name="Delete me", duration_value=1, duration_unit="week",
            resource_id=first_id,
        )
        old_unused = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=unused,
        )
        await save_owner_price(
            factory, actor_id=owner, project_id=project,
            plan_id=unused, currency_code="RUB", amount="9",
        )
        # Even with unchanged draft status, a price edit invalidates old delete.
        with pytest.raises(ValueError, match="изменился"):
            await delete_unused_owner_plan(
                factory, actor_id=owner, project_id=project,
                plan_id=unused, expected_status="draft",
                expected_revision=old_unused.revision,
            )
        with pytest.raises((TenantAccessDenied, PermissionError)):
            await delete_unused_owner_plan(
                factory, actor_id=outsider, project_id=project,
                plan_id=unused, expected_status="draft",
                expected_revision="old-confirmation",
            )
        await publish_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=unused,
        )
        active_info = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=unused,
        )
        await archive_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=unused,
            expected_revision=active_info.revision,
        )
        archived_info = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=unused,
        )
        await publish_owner_plan(
            factory, actor_id=owner, project_id=project, plan_id=unused,
            expected_status="archived",
            expected_revision=archived_info.revision,
        )
        # Archived + restored has the SAME status as the stale confirmation,
        # but it must still fail after the intervening lifecycle changes.
        with pytest.raises(ValueError, match="изменился"):
            await delete_unused_owner_plan(
                factory, actor_id=owner, project_id=project,
                plan_id=unused, expected_status="active",
                expected_revision=active_info.revision,
            )
        unused_info = await get_owner_plan_info(
            factory, actor_id=owner, project_id=project, plan_id=unused,
        )
        deleted = await delete_unused_owner_plan(
            factory, actor_id=owner, project_id=project,
            plan_id=unused, expected_status="active",
            expected_revision=unused_info.revision,
        )
        assert deleted == "Delete me"
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert await session.scalar(select(Plan).where(
                Plan.id == unused,
            )) is None
    finally:
        await engine.dispose()
