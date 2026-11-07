"""Real PostgreSQL: owner can manage obsolete central connections safely."""
import os
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.owner_setup.connection_management import (
    archive_owner_connection,
    get_connection_disposition,
    list_archived_owner_connections,
    remove_unused_connection,
    rename_owner_connection,
    restore_owner_connection,
)
from telegram_membership_saas.modules.owner_setup.obligation_audit import OwnerObligationAudit
from telegram_membership_saas.modules.owner_setup.service import (
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    ensure_telegram_actor,
    owner_home,
)
from telegram_membership_saas.modules.projects import Project, ProjectMember
from telegram_membership_saas.modules.resources import (
    Resource,
    ResourceStatus,
    ResourceType,
)
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL"), reason="Requires PostgreSQL tenant RLS",
)


def tg_id() -> int:
    return (uuid4().int % 8_000_000_000_000_000_000) + 1


async def test_unused_connection_with_configuration_can_be_deleted_but_no_other_project():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=tg_id(), username="connection_owner",
            first_name="Owner",
        )
        outsider = await ensure_telegram_actor(
            factory, telegram_id=tg_id(), username="connection_other",
            first_name="Other",
        )
        obsolete = await create_owner_project(
            factory, actor_id=owner, name="ViMi Pay E2E",
        )
        keep = await create_owner_project(
            factory, actor_id=owner, name="Another bot",
        )
        other = await create_owner_project(
            factory, actor_id=outsider, name="Outsider",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=obsolete),
        ) as session:
            resource = Resource(
                project_id=obsolete,
                resource_type=ResourceType.TELEGRAM_CHANNEL.value,
                external_id=str(-tg_id()),
                name="Unused channel", status=ResourceStatus.ACTIVE.value,
            )
            session.add(resource)
            await session.flush()
            resource_id = resource.id
        await create_owner_plan(
            factory, actor_id=owner, project_id=obsolete,
            name="Draft plan", duration_value=1, duration_unit="month",
            resource_id=resource_id,
        )
        await create_owner_payment_method(
            factory, actor_id=owner, project_id=obsolete,
            name="Old bank", currency_code="RUB",
            instruction="Pay {amount} {currency}.",
        )
        assert (await get_connection_disposition(
            factory, actor_id=owner, project_id=obsolete,
        )).has_history is False
        assert await rename_owner_connection(
            factory, actor_id=owner, project_id=obsolete,
            name="  ViMi Pay Test (unused)  ",
        ) == "ViMi Pay Test (unused)"
        with pytest.raises(ValueError, match="Название"):
            await rename_owner_connection(
                factory, actor_id=owner, project_id=obsolete, name=" ",
            )
        with pytest.raises(ValueError, match="Название"):
            await rename_owner_connection(
                factory, actor_id=owner, project_id=obsolete, name="line\nbreak",
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await rename_owner_connection(
                factory, actor_id=outsider, project_id=obsolete,
                name="stolen",
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await remove_unused_connection(
                factory, actor_id=outsider, project_id=obsolete,
            )
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await remove_unused_connection(
                factory, actor_id=owner, project_id=other,
            )
        # Member admins cannot remove another person's connection.
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=obsolete),
        ) as session:
            session.add(ProjectMember(
                project_id=obsolete, user_id=outsider, role="admin",
            ))
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await remove_unused_connection(
                factory, actor_id=outsider, project_id=obsolete,
            )
        await remove_unused_connection(
            factory, actor_id=owner, project_id=obsolete,
        )
        own = (await owner_home(factory, actor_id=owner)).projects
        assert (keep, "Another bot") in own
        assert all(pid != obsolete for pid, _ in own)
        assert (await owner_home(factory, actor_id=outsider)).projects == (
            (other, "Outsider"),
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner),
        ) as session:
            assert await session.get(Project, obsolete) is None
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await remove_unused_connection(
                factory, actor_id=owner, project_id=obsolete,
            )
    finally:
        await engine.dispose()


async def test_bot_attached_or_historical_connection_cannot_be_hard_deleted():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=tg_id(),
            username="another_connection_owner", first_name="Owner",
        )
        bot_project = await create_owner_project(
            factory, actor_id=owner, name="Still connected",
        )
        history_project = await create_owner_project(
            factory, actor_id=owner, name="Historic revenue",
        )
        bot_id = tg_id()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=bot_project),
        ) as session:
            session.add(BotInstance(
                project_id=bot_project, telegram_bot_id=bot_id,
                username="sample_connected_bot", display_name="Sample",
                token_ciphertext="test-only-no-real-token", status="registered",
            ))
        with pytest.raises(ValueError, match="отключите Telegram-бота"):
            await remove_unused_connection(
                factory, actor_id=owner, project_id=bot_project,
            )
        with pytest.raises(ValueError, match="отключите Telegram-бота"):
            await archive_owner_connection(
                factory, actor_id=owner, project_id=bot_project,
            )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=history_project),
        ) as session:
            audit_id = uuid4()
            session.add(OwnerObligationAudit(
                id=audit_id, project_id=history_project,
                actor_id=owner, action="close_payment",
                target_id=uuid4(), reason="Historical decision preserved",
            ))
        assert (await get_connection_disposition(
            factory, actor_id=owner, project_id=history_project,
        )).has_history
        with pytest.raises(ValueError, match="история"):
            await remove_unused_connection(
                factory, actor_id=owner, project_id=history_project,
            )
        await archive_owner_connection(
            factory, actor_id=owner, project_id=history_project,
        )
        assert all(pid != history_project for pid, _ in (
            await owner_home(factory, actor_id=owner)
        ).projects)
        assert (history_project, "Historic revenue") in (
            await list_archived_owner_connections(factory, actor_id=owner)
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=history_project),
        ) as session:
            assert await session.get(OwnerObligationAudit, audit_id)
        assert await restore_owner_connection(
            factory, actor_id=owner, project_id=history_project,
        ) == "Historic revenue"
        assert (history_project, "Historic revenue") in (
            await owner_home(factory, actor_id=owner)
        ).projects
        with pytest.raises(ValueError, match="архиве"):
            await restore_owner_connection(
                factory, actor_id=owner, project_id=history_project,
            )
    finally:
        await engine.dispose()


async def test_cancelled_transfer_history_forces_archive_not_delete():
    from telegram_membership_saas.modules.owner_setup.transfers import (
        cancel_transfer_invite,
        create_transfer_invite,
    )
    from sqlalchemy import select

    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=tg_id(), username="transfer_history_owner",
            first_name="Owner",
        )
        recipient_tid = tg_id()
        recipient = await ensure_telegram_actor(
            factory, telegram_id=recipient_tid, username="transfer_history_new",
            first_name="Recipient",
        )
        project = await create_owner_project(
            factory, actor_id=owner, name="Transfer history only",
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            bot = BotInstance(
                project_id=project, telegram_bot_id=tg_id(),
                username="historic_transfer_bot", display_name="History",
                token_ciphertext="test-only",
            )
            session.add(bot)
            await session.flush()
            bot_row_id = bot.id
        invite = await create_transfer_invite(
            factory, actor_id=owner, project_id=project,
            recipient_telegram_id=recipient_tid,
        )
        assert await cancel_transfer_invite(
            factory, actor_id=recipient, transfer_id=invite.id,
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            bot = await session.scalar(select(BotInstance).where(
                BotInstance.id == bot_row_id,
            ))
            await session.delete(bot)
        disposition = await get_connection_disposition(
            factory, actor_id=owner, project_id=project,
        )
        assert not disposition.has_bot
        assert disposition.has_history
        with pytest.raises(ValueError, match="архив"):
            await remove_unused_connection(
                factory, actor_id=owner, project_id=project,
            )
        await archive_owner_connection(
            factory, actor_id=owner, project_id=project,
        )
        assert (project, "Transfer history only") in (
            await list_archived_owner_connections(factory, actor_id=owner)
        )
    finally:
        await engine.dispose()
