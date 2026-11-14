"""Real PostgreSQL tests for dual-consent bot transfer and merchant continuity."""
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_owner_subscribers_rls import setup_subscriber, tg_id
from test_platform_saas_invoice_rls import _invoice, _line, _receipt

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots.models import BotInstance
from telegram_membership_saas.modules.bots.service import VerifiedBot
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayApp
from telegram_membership_saas.modules.crypto_pay.service import connect_crypto_pay
from telegram_membership_saas.modules.file_sales import service as file_service
from telegram_membership_saas.modules.file_sales.manual_models import FileManualOrder
from telegram_membership_saas.modules.owner_setup import transfers as svc
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.modules.owner_setup.transfer_models import (
    ProjectOwnershipTransfer,
)
from telegram_membership_saas.modules.payments.models import PaymentMethod
from telegram_membership_saas.modules.projects.models import Project
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext


async def factory_for_transfer_bot_id(factory, actor_id, project_id):
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        return await session.scalar(
            select(BotInstance.id).where(BotInstance.project_id == project_id)
        )


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
            old_crypto_app_id = uuid4()
            session.add(CryptoPayApp(
                id=old_crypto_app_id, project_id=project,
                telegram_bot_id=bot_id, owner_user_id=owner,
                token_ciphertext="encrypted-old-crypto-api-token",
                environment="testnet", app_label="Prior owner crypto app",
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

        # One unsettled service bill cannot be dodged through a transfer.
        # Once its verified bank settlement activates a paid month, the new
        # owner may accept the bot and must inherit the unused PAID coverage,
        # not the previous owner's financial obligation.
        bot_uuid = await factory_for_transfer_bot_id(factory, owner, project)
        operator_tg = tg_id()
        operator = await ensure_telegram_actor(
            factory, telegram_id=operator_tg,
            username=None, first_name=None,
        )
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "INSERT INTO platform_operators(telegram_user_id,role)"
                " VALUES(:tg,'owner')"
            ), {"tg": operator_tg})
            invoice = await _invoice(
                admin, owner, merchant_id, bot_uuid,
                period_start=datetime.now(UTC)-timedelta(days=15),
            )
            await _line(admin, invoice, project, "bot_fee", bot_uuid)
            await admin.execute(text(
                "UPDATE platform_saas_invoices SET status='issued'"
                " WHERE id=:id"
            ), {"id": invoice})
        with pytest.raises(ValueError, match="ViMi Pay"):
            await svc.finalize_transfer(
                factory, actor_id=owner, project_id=project,
                transfer_id=invitation.id,
            )
        async with admin_engine.begin() as admin:
            await admin.execute(text(
                "SELECT set_config('app.current_user_id',:actor,true)"
            ), {"actor": str(operator)})
            await _receipt(admin, invoice, operator)
        async with admin_engine.connect() as admin:
            coverage = (await admin.execute(text(
                "SELECT a.effective_end,i.period_end FROM "
                "platform_saas_bot_service_activations a "
                "JOIN platform_saas_invoices i ON i.id=a.invoice_id "
                "WHERE a.invoice_id=:id"
            ), {"id": invoice})).one()
            assert coverage.effective_end > coverage.period_end
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
            # FORCE RLS intentionally prevents new owner from reading the
            # outgoing owner's encrypted app, even after it was retired.
            assert await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.id == old_crypto_app_id,
            )) is None

        # A new merchant can connect a fresh Crypto Pay app without hitting
        # the one-active-app-per-bot index left by the old owner's credential.
        class NewMerchantProvider:
            def __init__(self, token, *, testnet):
                assert token == "123456789:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
                assert testnet is True

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def get_me(self):
                return {"name": "New merchant app"}

        new_crypto = await connect_crypto_pay(
            factory, project_id=project, bot_id=bot_id, actor_id=buyer,
            api_token="123456789:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
            encryption_key=SecretStr(Fernet.generate_key().decode("ascii")),
            testnet=True, client_factory=NewMerchantProvider,
        )
        assert new_crypto.app_label == "New merchant app"

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
            previous = (await session.execute(text(
                "SELECT status, token_ciphertext, retired_at "
                "FROM crypto_pay_apps WHERE id=:id"
            ), {"id": old_crypto_app_id})).one()
            assert previous[0] == "retired"
            assert previous[1] == "encrypted-old-crypto-api-token"
            assert previous[2] is not None
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


async def test_manual_file_liability_blocks_transfer_even_through_privileged_sql(monkeypatch):
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        owner, buyer, _, project, _, _, _, buyer_tid = await setup_subscriber(factory)
        bot_id = tg_id()
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(BotInstance(
                project_id=project, telegram_bot_id=bot_id,
                username="file_liability_bot", display_name="File liability",
                token_ciphertext="old-credential",
            ))
        folder = await file_service.add_folder(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, name="Files",
        )
        product = await file_service.create_product(
            factory, project_id=project, bot_id=bot_id,
            actor_id=owner, folder_id=folder.id, name="Video", stars=1,
        )
        transfer = await svc.create_transfer_invite(
            factory, actor_id=owner, project_id=project,
            recipient_telegram_id=buyer_tid,
        )

        async def fake_verify(_token):
            return VerifiedBot(
                telegram_bot_id=bot_id, username="file_liability_bot",
                display_name="File liability",
            )

        async def fake_channel_access(*_args, **_kwargs):
            return None

        monkeypatch.setattr(svc, "verify_telegram_bot", fake_verify)
        monkeypatch.setattr(svc, "_verify_recipient_channel_admin", fake_channel_access)
        monkeypatch.setattr(svc, "encrypt_token", lambda token, key: "new-credential")
        await svc.accept_transfer_invite(
            factory, actor_id=buyer, actor_telegram_id=buyer_tid,
            transfer_id=transfer.id, fresh_token="fake-token",
            encryption_key=None,
        )
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project),
        ) as session:
            session.add(FileManualOrder(
                project_id=project, telegram_bot_id=bot_id,
                telegram_user_id=buyer_tid, product_id=product.id,
                product_name="Video", amount=1, currency_code="RUB",
                payment_method_id=uuid4(), payment_method_name="Bank",
                instruction="Old owner bank", status="pending",
            ))
        with pytest.raises(ValueError, match="незавершённые оплаты файлов"):
            await svc.finalize_transfer(
                factory, actor_id=owner, project_id=project,
                transfer_id=transfer.id,
            )
        # Even a direct call of the narrowly privileged transfer function
        # cannot bypass the DB-level file liability trigger.
        with pytest.raises(DBAPIError):
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner, project_id=project),
            ) as session:
                bot = await session.scalar(select(BotInstance).where(
                    BotInstance.project_id == project,
                    BotInstance.telegram_bot_id == bot_id,
                ))
                bot.token_ciphertext = "new-credential"
                await session.flush()
                await session.execute(text(
                    "SELECT public.complete_bot_ownership_transfer(:pid, :tid)"
                ), {"pid": project, "tid": transfer.id})
    finally:
        await engine.dispose()
