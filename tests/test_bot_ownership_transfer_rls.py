"""Real PostgreSQL tests for dual-consent bot transfer and merchant continuity."""
import os

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_owner_subscribers_rls import setup_subscriber, tg_id

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots.models import BotInstance
from telegram_membership_saas.modules.bots.service import VerifiedBot
from telegram_membership_saas.modules.owner_setup import transfers as svc
from telegram_membership_saas.modules.owner_setup.transfer_models import (
    ProjectOwnershipTransfer,
)
from telegram_membership_saas.modules.payments.models import PaymentMethod
from telegram_membership_saas.modules.projects.models import Project
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL")
    or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Requires migrated PostgreSQL and migration database connection",
)


async def test_owner_transfer_requires_both_parties_and_keeps_merchant_history(monkeypatch):
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    try:
        owner, buyer, outsider, project, other, cid, resource, buyer_tid = (
            await setup_subscriber(factory)
        )
        bot_id = tg_id()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            project_row = await session.scalar(select(Project).where(
                Project.id == project,
            ))
            merchant_id = project_row.merchant_id
            session.add(BotInstance(
                project_id=project, telegram_bot_id=bot_id,
                username="test_transfer_bot", display_name="Test Bot",
                token_ciphertext="old-credential",
            ))
            session.add(PaymentMethod(
                project_id=project, name="Owner bank",
                instruction_template="Old owner bank account",
                status="active",
            ))

        with pytest.raises((PermissionError, TenantAccessDenied)):
            await svc.create_transfer_invite(
                factory, actor_id=outsider, project_id=project,
                recipient_telegram_id=buyer_tid,
            )
        invitation = await svc.create_transfer_invite(
            factory, actor_id=owner, project_id=project,
            recipient_telegram_id=buyer_tid,
        )
        assert invitation.status == "pending"
        assert invitation.to_user_id == buyer
        assert invitation.project_id == project
        assert invitation.bot_id == bot_id
        assert len(await svc.list_transfer_invites(factory, actor_id=owner)) == 1
        assert len(await svc.list_transfer_invites(factory, actor_id=buyer)) == 1
        assert await svc.list_transfer_invites(factory, actor_id=outsider) == ()

        with pytest.raises((PermissionError, TenantAccessDenied)):
            await svc.get_transfer_invite(
                factory, actor_id=outsider, transfer_id=invitation.id,
            )
        # SQL injection of a status change, and direct owner reassignment, fail.
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.execute(text(
                    "UPDATE project_ownership_transfers "
                    "SET status = 'accepted', encrypted_new_token = 'forged', "
                    "accepted_at = now() WHERE id = :id"
                ), {"id": invitation.id})
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.execute(text(
                    "UPDATE projects SET owner_user_id = :target WHERE id = :pid"
                ), {"target": buyer, "pid": project})

        async def fake_verify(token):
            return VerifiedBot(
                telegram_bot_id=bot_id, username="test_transfer_bot",
                display_name="Test Bot",
            )

        async def fake_channel_access(factory, *, transfer, recipient_telegram_id, token):
            assert transfer.bot_id == bot_id
            assert recipient_telegram_id == buyer_tid
            assert token == "FAKE_TEST_TOKEN"

        monkeypatch.setattr(svc, "verify_telegram_bot", fake_verify)
        monkeypatch.setattr(svc, "_verify_recipient_channel_admin", fake_channel_access)
        monkeypatch.setattr(svc, "encrypt_token", lambda token, key: "fresh-credential")
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await svc.accept_transfer_invite(
                factory, actor_id=outsider, actor_telegram_id=tg_id(),
                transfer_id=invitation.id, fresh_token="FAKE_TEST_TOKEN",
                encryption_key=None,
            )
        accepted = await svc.accept_transfer_invite(
            factory, actor_id=buyer, actor_telegram_id=buyer_tid,
            transfer_id=invitation.id, fresh_token="FAKE_TEST_TOKEN",
            encryption_key=None,
        )
        assert accepted.status == "accepted"
        # Even after the recipient accepts, direct SQL owner reassignment
        # cannot skip disabling the old payment methods and changing the bot token.
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                await session.execute(text(
                    "UPDATE projects SET owner_user_id = :target WHERE id = :pid"
                ), {"target": buyer, "pid": project})

        result = await svc.finalize_transfer(
            factory, actor_id=owner, project_id=project, transfer_id=invitation.id,
        )
        assert result.status == "completed"
        assert await svc.list_transfer_invites(factory, actor_id=owner) == ()
        assert await svc.list_transfer_invites(factory, actor_id=buyer) == ()
        # Owner access changes; other projects stay with their original owners.
        async with tenant_session_scope(
            factory, TenantContext(user_id=buyer, project_id=project),
        ) as session:
            p = await session.scalar(select(Project).where(Project.id == project))
            assert p.owner_user_id == buyer
            assert p.merchant_id == merchant_id
            methods = (await session.scalars(select(PaymentMethod).where(
                PaymentMethod.project_id == project,
            ))).all()
            assert len(methods) == 1
            assert methods[0].status == "disabled"
            assert methods[0].instruction_template == "Old owner bank account"
            bot = await session.scalar(select(BotInstance).where(
                BotInstance.project_id == project,
            ))
            assert bot.token_ciphertext == "fresh-credential"
            transferred = await session.scalar(select(ProjectOwnershipTransfer).where(
                ProjectOwnershipTransfer.id == invitation.id,
            ))
            assert transferred.status == "completed"
            assert transferred.encrypted_new_token is None

        with pytest.raises((PermissionError, TenantAccessDenied)):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ):
                pass
        async with tenant_session_scope(
            factory, TenantContext(user_id=outsider, project_id=other),
        ) as session:
            assert await session.scalar(select(Project.owner_user_id).where(
                Project.id == other,
            )) == outsider

        async with admin_engine.connect() as session:
            periods = (await session.execute(text(
                "SELECT merchant_id, owner_user_id, started_at, ended_at "
                "FROM merchant_project_periods WHERE project_id = :pid "
                "ORDER BY started_at"
            ), {"pid": project})).all()
            assert len(periods) == 2
            assert all(row[0] == merchant_id for row in periods)
            assert periods[0][1] == owner and periods[0][3] is not None
            assert periods[1][1] == buyer and periods[1][3] is None
    finally:
        await admin_engine.dispose()
        await engine.dispose()


async def test_transfer_cancelled_invitation_cannot_be_accepted():
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        owner, buyer, _, project, _, _, _, buyer_tid = await setup_subscriber(factory)
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(BotInstance(
                project_id=project, telegram_bot_id=tg_id(),
                username="cancel_test_bot", display_name="Cancel Test",
                token_ciphertext="old-ciphertext",
            ))
        invited = await svc.create_transfer_invite(
            factory, actor_id=owner, project_id=project,
            recipient_telegram_id=buyer_tid,
        )
        assert await svc.cancel_transfer_invite(
            factory, actor_id=buyer, transfer_id=invited.id,
        )
        assert not await svc.cancel_transfer_invite(
            factory, actor_id=owner, transfer_id=invited.id,
        )
        row = await svc.get_transfer_invite(
            factory, actor_id=owner, transfer_id=invited.id,
        )
        assert row.status == "cancelled"
    finally:
        await engine.dispose()
