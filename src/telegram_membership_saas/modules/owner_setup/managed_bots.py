"""Telegram Bot API Managed Bots onboarding and owner-verified token synchronization.

Only the *central* ViMi Pay bot may use these operations. Telegram owns the
newly-created bot; users explicitly consent in the native creation dialog.
Tokens never enter a chat, log, URL, callback or a plain database column.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from aiogram import Bot
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import (
    BotRegistration,
    decrypt_token,
    verify_telegram_bot,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.owner_setup.models import OwnerSetupSession
from telegram_membership_saas.modules.owner_setup.service import (
    consume_button_flow,
    register_new_owner_bot,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.tenant_context import TenantContext


@dataclass(frozen=True, slots=True)
class ManagedBotResult:
    kind: str  # created / refreshed / unchanged / ignored
    username: str = ""
    project_id: UUID | None = None
    actor_id: UUID | None = None


async def _registered_bot(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, managed_bot_id: int,
) -> tuple[UUID, str] | None:
    """Find one owner's bot across tenant scopes without bypassing RLS.

    BotInstance is intentionally hidden unless app.project_id is set.
    Query only owner-visible projects, then inspect their registered bot
    inside each exact tenant scope. Never query bots in a global DB session.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        projects = (await session.scalars(select(Project.id).where(
            Project.owner_user_id == actor_id,
            Project.status == "active",
        ))).all()
    for project_id in projects:
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id),
        ) as session:
            encrypted = await session.scalar(
                select(BotInstance.token_ciphertext).where(
                    BotInstance.project_id == project_id,
                    BotInstance.telegram_bot_id == managed_bot_id,
                    BotInstance.status == "registered",
                )
            )
            if encrypted is not None:
                return project_id, encrypted
    return None


async def _actor_for_telegram_id(
    factory: async_sessionmaker[AsyncSession], user_id: int,
) -> UUID | None:
    if user_id <= 0:
        return None
    async with factory() as session:
        return await session.scalar(select(TelegramUser.id).where(
            TelegramUser.telegram_user_id == user_id,
        ))


async def managed_creation_pending(
    factory: async_sessionmaker[AsyncSession], actor_id: UUID,
) -> bool:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        flow = await session.get(OwnerSetupSession, (actor_id, 0))
        return bool(
            flow is not None and flow.step == "managed_bot_create"
            and flow.expires_at > datetime.now(UTC)
        )


async def managed_connected_bot(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, managed_bot_id: int,
) -> UUID | None:
    """Ensure the managed bot belongs to this particular ViMi Pay owner."""
    row = await _registered_bot(
        factory, actor_id=actor_id, managed_bot_id=managed_bot_id,
    )
    return row[0] if row is not None else None


async def stage_managed_bot_candidate(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, managed_bot_id: int,
) -> str | None:
    """Stage Telegram service-message candidate for explicit owner confirmation.

    Telegram does not include the request_id in ManagedBotCreated or
    ManagedBotUpdated, so we MUST NOT consume a creation intent from either.
    A pending owner can accept only this exact candidate and generation.
    """
    if managed_bot_id <= 0:
        return None
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        state = await session.get(
            OwnerSetupSession, (actor_id, 0), with_for_update=True,
        )
        if (
            state is None or state.step != "managed_bot_create"
            or state.expires_at <= datetime.now(UTC)
            or not state.generation
        ):
            return None
        payload = dict(state.payload or {})
        # A delayed second notification cannot replace the already shown
        # candidate. Owner may cancel and start a fresh request.
        if payload.get("candidate_bot_id") not in (None, managed_bot_id):
            return None
        payload["candidate_bot_id"] = managed_bot_id
        state.payload = payload
        return state.generation


async def pending_managed_candidate(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, managed_bot_id: int, generation: str,
) -> bool:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        flow = await session.get(OwnerSetupSession, (actor_id, 0))
        return bool(
            flow is not None and flow.step == "managed_bot_create"
            and flow.expires_at > datetime.now(UTC)
            and flow.generation == generation
            and (flow.payload or {}).get("candidate_bot_id") == managed_bot_id
        )


async def connect_managed_bot_event(
    factory: async_sessionmaker[AsyncSession], *,
    central_bot: Bot,
    creator_telegram_id: int,
    managed_bot_id: int,
    encryption_key: SecretStr | None,
    generation: str,
) -> ManagedBotResult:
    """Onboard only a new bot explicitly requested by its Telegram creator.

    Called ONLY after the owner clicks Confirm on a real Telegram service
    message candidate. The callback generation and candidate are checked
    again under the durable wizard lock. Raw managed_bot updates never
    consume onboarding intents or rotate existing tokens.
    """
    if creator_telegram_id <= 0 or managed_bot_id <= 0:
        return ManagedBotResult(kind="ignored")
    actor_id = await _actor_for_telegram_id(factory, creator_telegram_id)
    if actor_id is None:
        return ManagedBotResult(kind="ignored")
    if await managed_connected_bot(
        factory, actor_id=actor_id, managed_bot_id=managed_bot_id,
    ) is not None:
        return ManagedBotResult(kind="ignored")
    if not await pending_managed_candidate(
        factory, actor_id=actor_id, managed_bot_id=managed_bot_id,
        generation=generation,
    ):
        return ManagedBotResult(kind="ignored")

    # Telegram authorizes manager access. Never accept a user-supplied token
    # or bot ID without checking the returned token's actual getMe identity.
    token = await central_bot.get_managed_bot_token(user_id=managed_bot_id)
    verified = await verify_telegram_bot(token)
    if verified.telegram_bot_id != managed_bot_id:
        raise ValueError("Managed bot identity does not match Telegram token")
    try:
        await consume_button_flow(
            factory, actor_id=actor_id, expected_step="managed_bot_create",
            generation=generation, bot_scope_id=0,
        )
    except ValueError:
        # Another update or a user cancel won the wizard row lock.
        return ManagedBotResult(kind="ignored")

    project_id, registration = await register_new_owner_bot(
        factory, actor_id=actor_id,
        actor_telegram_user_id=creator_telegram_id,
        token=token, encryption_key=encryption_key,
        central_bot_id=central_bot.id,
        expected_bot_id=managed_bot_id,
    )
    return ManagedBotResult(
        kind="created", username=registration.username,
        project_id=project_id, actor_id=actor_id,
    )


async def managed_bot_id_for_project(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> int:
    """Look up the exact live customer bot by owned project, not user input."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        bot_id = await session.scalar(select(BotInstance.telegram_bot_id).join(
            Project, Project.id == BotInstance.project_id,
        ).where(
            Project.id == project_id,
            Project.owner_user_id == actor_id,
            Project.status == "active",
            BotInstance.status == "registered",
        ))
        if bot_id is None:
            raise PermissionError("No active bot owned by this user")
        return bot_id


async def sync_managed_bot_token(
    factory: async_sessionmaker[AsyncSession], *,
    central_bot: Bot, actor_id: UUID, actor_telegram_id: int,
    managed_bot_id: int, encryption_key: SecretStr | None,
) -> ManagedBotResult:
    """Explicit owner-only refresh. Never rotate the token at Telegram."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        tg_id = await session.scalar(select(TelegramUser.telegram_user_id).where(
            TelegramUser.id == actor_id,
        ))
        if tg_id != actor_telegram_id:
            raise PermissionError("Managed bot owner identity mismatch")
    current = await _registered_bot(
        factory, actor_id=actor_id, managed_bot_id=managed_bot_id,
    )
    if current is None:
        raise PermissionError("This bot isn't registered to this owner")

    project_id, encrypted_token = current
    token = await central_bot.get_managed_bot_token(user_id=managed_bot_id)
    verified = await verify_telegram_bot(token)
    if verified.telegram_bot_id != managed_bot_id:
        raise ValueError("Managed bot identity does not match Telegram token")

    # An unchanged token should not touch the DB or restart the worker.
    if decrypt_token(encrypted_token, key=encryption_key) == token:
        return ManagedBotResult(
            kind="unchanged", username=verified.username, project_id=project_id,
        )
    # Existing registration method enforces project ownership, one bot per
    # project and global bot-ID uniqueness before saving encrypted credentials.
    from telegram_membership_saas.modules.bots.service import register_customer_bot

    result: BotRegistration = await register_customer_bot(
        factory, project_id=project_id,
        actor_telegram_user_id=actor_telegram_id,
        token=token, encryption_key=encryption_key,
        central_bot_id=central_bot.id,
    )
    if result.telegram_bot_id != managed_bot_id:
        raise ValueError("Managed bot changed during synchronization")
    return ManagedBotResult(
        kind="refreshed", username=result.username, project_id=project_id,
    )
