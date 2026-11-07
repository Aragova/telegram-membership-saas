"""PostgreSQL RLS: support cannot escalate to financial or owner actions."""
import os

import pytest
from sqlalchemy import func, select

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine, build_session_factory, tenant_session_scope,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.owner_setup.moderators import (
    can_use_moderator_panel,
    find_moderator_customer,
    list_moderator_customers,
    list_moderator_reviews,
    list_project_moderators,
    project_support_recipients,
    set_moderated_customer_ban,
    set_project_moderator,
)
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.owner_setup.subscribers import (
    grant_manual_access, list_subscribers, set_customer_ban,
)
from telegram_membership_saas.modules.payments import Payment, PaymentRequest
from telegram_membership_saas.modules.payments.request_service import (
    require_payment_permission,
)
from telegram_membership_saas.modules.projects import Permission, ProjectMember
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext
from test_owner_subscribers_rls import setup_subscriber, tg_id

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires migrated PostgreSQL",
)


async def test_support_is_project_local_can_ban_and_never_manage_payments_or_access():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, other, cid, resource, buyer_tid = (
            await setup_subscriber(factory)
        )
        staff_tid = tg_id()
        staff = await ensure_telegram_actor(
            factory, telegram_id=staff_tid, username="mod_test",
            first_name="Moderator",
        )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await set_project_moderator(
                factory, actor_id=outsider, project_id=project,
                telegram_id=staff_tid, enabled=True,
            )
        assert await set_project_moderator(
            factory, actor_id=owner, project_id=project,
            telegram_id=staff_tid, enabled=True,
        )
        assert not await set_project_moderator(
            factory, actor_id=owner, project_id=project,
            telegram_id=staff_tid, enabled=True,
        )
        assert await can_use_moderator_panel(
            factory, actor_id=staff, project_id=project, support_only=True,
        )
        assert not await can_use_moderator_panel(
            factory, actor_id=staff, project_id=other, support_only=True,
        )
        assert staff_tid in await project_support_recipients(
            factory, project_id=project,
        )
        total, moderators = await list_project_moderators(
            factory, actor_id=owner, project_id=project,
        )
        assert total == 1 and moderators[0].telegram_id == staff_tid
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await list_project_moderators(
                factory, actor_id=staff, project_id=project,
            )
        total, users = await list_moderator_customers(
            factory, actor_id=staff, project_id=project,
        )
        assert total >= 1 and any(x.id == cid for x in users)
        found = await find_moderator_customer(
            factory, actor_id=staff, project_id=project, telegram_id=buyer_tid,
        )
        assert found.id == cid and found.invoice_count == 0
        with pytest.raises((PermissionError, ValueError, TenantAccessDenied)):
            await find_moderator_customer(
                factory, actor_id=staff, project_id=other, telegram_id=buyer_tid,
            )
        assert await list_moderator_reviews(
            factory, actor_id=staff, project_id=project,
        ) == ()

        # Support may change project-local ban, not financial or physical access.
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await set_customer_ban(
                factory, actor_id=staff, project_id=project,
                customer_id=cid, banned=True,
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await grant_manual_access(
                factory, actor_id=staff, project_id=project,
                customer_id=cid, resource_id=resource, duration_days=7,
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await list_subscribers(
                factory, actor_id=staff, project_id=project,
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=staff, project_id=project),
        ) as session:
            with pytest.raises(PermissionError):
                await require_payment_permission(
                    session, project_id=project, actor_user_id=staff,
                    permission=Permission.PAYMENT_APPROVE,
                )

        assert await set_moderated_customer_ban(
            factory, actor_id=staff, project_id=project,
            customer_id=cid, banned=True,
        )
        assert (await find_moderator_customer(
            factory, actor_id=staff, project_id=project, customer_id=cid,
        )).is_banned
        assert await set_moderated_customer_ban(
            factory, actor_id=staff, project_id=project,
            customer_id=cid, banned=False,
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            assert await session.scalar(
                select(func.count(Payment.id)).where(Payment.project_id == project)
            ) == 0
            assert await session.scalar(
                select(func.count(PaymentRequest.id)).where(
                    PaymentRequest.project_id == project,
                )
            ) == 0
        assert await set_project_moderator(
            factory, actor_id=owner, project_id=project,
            telegram_id=staff_tid, enabled=False,
        )
        assert not await can_use_moderator_panel(
            factory, actor_id=staff, project_id=project,
        )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await find_moderator_customer(
                factory, actor_id=staff, project_id=project, customer_id=cid,
            )
    finally:
        await engine.dispose()
