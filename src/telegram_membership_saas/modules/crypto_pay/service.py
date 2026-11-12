"""Owner-protected encrypted Crypto Pay app registration.

No public buyer buttons until authenticated invoice -> full entitlement/file
delivery is implemented. Retiring an app does NOT delete its historical token
or pending invoices: late provider confirmation must remain reconcilable.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import decrypt_token, encrypt_token
from telegram_membership_saas.modules.crypto_pay.client import CryptoPayClient, CryptoPayError
from telegram_membership_saas.modules.crypto_pay.models import CryptoPayApp
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class CryptoPayConnection:
    app_id: UUID
    app_label: str
    environment: str
    enabled: bool


async def _assert_owner_bot(
    session: AsyncSession, *, project_id: UUID,
    actor_id: UUID, bot_id: int,
) -> None:
    owner = await session.scalar(select(Project.owner_user_id).where(
        Project.id == project_id, Project.status == "active",
    ))
    bot = await session.scalar(select(BotInstance.id).where(
        BotInstance.project_id == project_id,
        BotInstance.telegram_bot_id == bot_id,
        BotInstance.status == "registered",
    ))
    if owner != actor_id or bot is None:
        raise PermissionError("Only the current connected-bot owner can set up Crypto Pay")


async def read_crypto_pay_connection(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
) -> CryptoPayConnection | None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _assert_owner_bot(
            session, project_id=project_id, actor_id=actor_id, bot_id=bot_id,
        )
        app = await session.scalar(select(CryptoPayApp).where(
            CryptoPayApp.project_id == project_id,
            CryptoPayApp.telegram_bot_id == bot_id,
            CryptoPayApp.owner_user_id == actor_id,
            CryptoPayApp.status == "active",
        ))
        if app is None:
            return None
        return CryptoPayConnection(
            app_id=app.id, app_label=app.app_label,
            environment=app.environment, enabled=True,
        )


async def connect_crypto_pay(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
    api_token: str, encryption_key: SecretStr | None,
    testnet: bool = False,
    client_factory: Callable[..., CryptoPayClient] = CryptoPayClient,
) -> CryptoPayConnection:
    """Verify ownership BEFORE external call, and recheck with project lock.

    A new version replaces the active credential, retaining retired ones for
    old provider invoices. Never return or log API tokens or ciphertexts.
    """
    if len(api_token) < 20 or len(api_token) > 256 or not api_token.isascii():
        raise ValueError("Crypto Pay API token format is invalid")
    if any(c.isspace() for c in api_token):
        raise ValueError("Crypto Pay API token format is invalid")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await _assert_owner_bot(
            session, project_id=project_id, actor_id=actor_id, bot_id=bot_id,
        )
    # Encryption configured by existing secure bot-token infrastructure.
    ciphertext = encrypt_token(api_token, key=encryption_key)
    async with client_factory(api_token, testnet=testnet) as api:
        identity = await api.get_me()
    label = identity.get("name")
    if not isinstance(label, str) or not label.strip():
        label = "Crypto Pay"
    label = label.strip()[:120]

    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _assert_owner_bot(
            session, project_id=project_id, actor_id=actor_id, bot_id=bot_id,
        )
        existing = (await session.scalars(select(CryptoPayApp).where(
            CryptoPayApp.project_id == project_id,
            CryptoPayApp.telegram_bot_id == bot_id,
            CryptoPayApp.status == "active",
        ).with_for_update())).all()
        for old in existing:
            old.status = "retired"
            old.retired_at = datetime.now(UTC)
        app = CryptoPayApp(
            project_id=project_id,
            telegram_bot_id=bot_id, owner_user_id=actor_id,
            token_ciphertext=ciphertext, environment="testnet" if testnet else "mainnet",
            app_label=label, status="active",
        )
        session.add(app)
        await session.flush()
        return CryptoPayConnection(
            app_id=app.id, app_label=label,
            environment=app.environment, enabled=True,
        )


async def disconnect_crypto_pay(
    factory: async_sessionmaker[AsyncSession], *,
    project_id: UUID, bot_id: int, actor_id: UUID,
) -> bool:
    """Stop NEW invoices only. Keep old app credentials for pending invoices."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _assert_owner_bot(
            session, project_id=project_id, actor_id=actor_id, bot_id=bot_id,
        )
        app = await session.scalar(select(CryptoPayApp).where(
            CryptoPayApp.project_id == project_id,
            CryptoPayApp.telegram_bot_id == bot_id,
            CryptoPayApp.owner_user_id == actor_id,
            CryptoPayApp.status == "active",
        ).with_for_update())
        if app is None:
            return False
        app.status = "retired"
        app.retired_at = datetime.now(UTC)
        return True


async def load_crypto_app_token(
    session: AsyncSession, *, project_id: UUID, app_id: UUID,
    encryption_key: SecretStr | None,
) -> tuple[CryptoPayApp, str]:
    """Trusted worker only; caller must already use system PROJECT scope.

    Do not use this from a tenant UI or log a decrypted credential.
    Historical retired app tokens are deliberately available to settlement.
    """
    app = await session.scalar(select(CryptoPayApp).where(
        CryptoPayApp.project_id == project_id, CryptoPayApp.id == app_id,
    ))
    if app is None:
        raise CryptoPayError("Crypto Pay app is not available in this project")
    return app, decrypt_token(app.token_ciphertext, key=encryption_key)
