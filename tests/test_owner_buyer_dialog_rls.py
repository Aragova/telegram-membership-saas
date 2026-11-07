"""Buyer conversation lookup honors the connected project's tenant boundary."""
import os

import pytest
from sqlalchemy import select
from test_owner_subscribers_rls import setup_subscriber

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.identity.models import ProjectCustomer
from telegram_membership_saas.modules.owner_setup.conversations import (
    find_buyer_conversation,
)
from telegram_membership_saas.tenant_context import TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"),
    reason="Requires migrated PostgreSQL",
)


async def test_dialog_routes_only_project_customer_to_project_owner():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner, _, outsider, project, other, cid, _, buyer_tid = (
            await setup_subscriber(factory)
        )
        info = await find_buyer_conversation(
            factory, project_id=project, telegram_id=buyer_tid,
        )
        assert info is not None and info.customer_id == cid
        assert info.customer_telegram_id == buyer_tid
        assert info.owner_telegram_id > 0
        assert info.is_banned is False

        # Same Telegram ID must never route a message through another bot.
        assert await find_buyer_conversation(
            factory, project_id=other, telegram_id=buyer_tid,
        ) is None

        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            customer = await session.scalar(
                select(ProjectCustomer).where(
                    ProjectCustomer.id == cid,
                    ProjectCustomer.project_id == project,
                )
            )
            customer.is_banned = True
        blocked = await find_buyer_conversation(
            factory, project_id=project, telegram_id=buyer_tid,
        )
        assert blocked is not None and blocked.is_banned is True
    finally:
        await engine.dispose()
