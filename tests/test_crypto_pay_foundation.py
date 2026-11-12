"""Real PostgreSQL RLS and credential rotation for Crypto Pay merchant apps."""
import os
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select
from test_platform_saas_invoice_rls import _project, _tid

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import decrypt_token
from telegram_membership_saas.modules.crypto_pay.models import (
    CryptoPayApp,
    CryptoPayInvoice,
)
from telegram_membership_saas.modules.crypto_pay.service import (
    connect_crypto_pay,
    disconnect_crypto_pay,
    read_crypto_pay_connection,
)
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

pytestmark = pytest.mark.skipif(
    not os.getenv("TMS_DATABASE_URL") or not os.getenv("TMS_MIGRATION_DATABASE_URL"),
    reason="Migrated PostgreSQL with non-bypass RLS required",
)
TOKEN1 = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TOKEN2 = "123456789:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


class FakeProvider:
    def __init__(self, token: str, *, testnet: bool):
        self.token = token
        self.testnet = testnet

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get_me(self):
        return {"name": "Merchant test app"}


async def test_crypto_pay_credentials_are_tenant_owner_only_and_versioned():
    engine = build_engine(Settings())
    factory = build_session_factory(engine)
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    try:
        owner, _, project_id, bot_uuid, _ = await _project(factory, _tid())
        stranger, _, other_project, other_bot_uuid, _ = await _project(factory, _tid())
        async with system_project_session_scope(factory, project_id) as session:
            bot_id = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == bot_uuid,
            ))
        async with system_project_session_scope(factory, other_project) as session:
            other_bot = await session.scalar(select(BotInstance.telegram_bot_id).where(
                BotInstance.id == other_bot_uuid,
            ))
        with pytest.raises((PermissionError, TenantAccessDenied)):
            await connect_crypto_pay(
                factory, project_id=project_id, bot_id=bot_id, actor_id=stranger,
                api_token=TOKEN1, encryption_key=key,
                client_factory=FakeProvider,
            )
        result = await connect_crypto_pay(
            factory, project_id=project_id, bot_id=bot_id, actor_id=owner,
            api_token=TOKEN1, encryption_key=key, testnet=True,
            client_factory=FakeProvider,
        )
        assert result.app_label == "Merchant test app"
        assert result.environment == "testnet"
        assert (await read_crypto_pay_connection(
            factory, project_id=project_id, bot_id=bot_id, actor_id=owner,
        )).app_id == result.app_id
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id),
        ) as session:
            old = await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.id == result.app_id,
            ))
            assert old.token_ciphertext != TOKEN1
            assert TOKEN1 not in old.token_ciphertext
            assert decrypt_token(old.token_ciphertext, key=key) == TOKEN1

        rotated = await connect_crypto_pay(
            factory, project_id=project_id, bot_id=bot_id, actor_id=owner,
            api_token=TOKEN2, encryption_key=key, testnet=False,
            client_factory=FakeProvider,
        )
        assert rotated.app_id != result.app_id
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner, project_id=project_id),
        ) as session:
            older = await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.id == result.app_id,
            ))
            assert older.status == "retired"
            assert older.retired_at is not None
            assert decrypt_token(older.token_ciphertext, key=key) == TOKEN1

            # The invoice is only RESERVED; no paid access can be made by
            # connecting or rotating a credential.
            invoice = CryptoPayInvoice(
                project_id=project_id, app_id=rotated.app_id,
                telegram_bot_id=bot_id, telegram_user_id=_tid(),
                kind="file", product_id=uuid4(),
                name_snapshot="Paid file", amount="99.00",
                currency_code="RUB", currency_type="fiat",
                payload="cp:" + uuid4().hex, status="reserved",
            )
            session.add(invoice)
            await session.flush()
            assert invoice.provider_invoice_id is None
            assert invoice.paid_at is None
        async with system_project_session_scope(factory, other_project) as session:
            # A trusted worker is still scoped to its single project.
            assert await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.id == rotated.app_id,
            )) is None
            assert await session.scalar(select(CryptoPayInvoice).where(
                CryptoPayInvoice.project_id == project_id,
            )) is None
        assert await read_crypto_pay_connection(
            factory, project_id=other_project, bot_id=other_bot,
            actor_id=stranger,
        ) is None
        assert await disconnect_crypto_pay(
            factory, project_id=project_id, bot_id=bot_id, actor_id=owner,
        )
        assert await read_crypto_pay_connection(
            factory, project_id=project_id, bot_id=bot_id, actor_id=owner,
        ) is None
        # Retired rows must survive checkout switch-off for delayed settlement.
        async with system_project_session_scope(factory, project_id) as session:
            assert await session.scalar(select(CryptoPayApp).where(
                CryptoPayApp.id == rotated.app_id,
                CryptoPayApp.status == "retired",
            )) is not None
    finally:
        await engine.dispose()
