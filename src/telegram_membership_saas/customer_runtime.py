"""Single-project customer-facing BotInstance polling runtime.

The first safe step toward multi-bot: one customer bot per project, in a
separate process from central ViMi Pay. Never run several access workers for
the same project's resources until actions are routed by BotInstance.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from aiogram import Bot
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    system_project_session_scope,
)
from telegram_membership_saas.modules.bots import BotInstance, BotInstanceStatus
from telegram_membership_saas.modules.bots.service import decrypt_token
from telegram_membership_saas.modules.projects import Project, ProjectStatus
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.telegram_access import TelegramAccessAdapter
from telegram_membership_saas.telegram_adapter import create_customer_dispatcher
from telegram_membership_saas.telegram_runtime import run_project_runtime_loop

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CustomerBotConfig:
    project_id: UUID
    telegram_bot_id: int
    token: str


async def load_customer_bot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    telegram_bot_id: int,
    settings: Settings,
) -> CustomerBotConfig:
    """Load ciphertext only through a trusted single-project RLS scope."""
    if settings.bot_token_encryption_key is None:
        raise RuntimeError("Customer bot encryption key is not configured")

    async with system_project_session_scope(session_factory, project_id) as session:
        status = await session.scalar(select(Project.status).where(Project.id == project_id))
        if status != ProjectStatus.ACTIVE.value:
            raise RuntimeError("Customer bot project is not active")

        records = list(
            (await session.scalars(
                select(BotInstance).where(
                    BotInstance.project_id == project_id,
                    BotInstance.status == BotInstanceStatus.REGISTERED.value,
                )
            )).all()
        )
        # Existing access tables are project/resource scoped, not bot scoped.
        # Multiple bot workers for one project could deliver an invite/revoke
        # with the wrong Telegram identity. Fail closed until access routing
        # adds a bot_instance_id to the relevant operations.
        if len(records) != 1 or records[0].telegram_bot_id != telegram_bot_id:
            raise RuntimeError(
                "Project requires exactly one registered customer bot for this runtime"
            )
        ciphertext = records[0].token_ciphertext

    try:
        token = decrypt_token(ciphertext, key=settings.bot_token_encryption_key)
    except (InvalidToken, ValueError, TypeError):
        raise RuntimeError("Customer bot credentials cannot be decrypted") from None
    return CustomerBotConfig(
        project_id=project_id, telegram_bot_id=telegram_bot_id, token=token
    )


async def check_customer_bot_identity(bot: Bot, telegram_bot_id: int) -> None:
    # Never include transport details (which can contain a token-bearing URL).
    try:
        actual = await bot.get_me()
        if actual.id != telegram_bot_id or not actual.is_bot:
            raise ValueError("Incorrect Telegram bot identity")
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ValueError("Telegram webhook is already configured")
    except Exception:
        raise RuntimeError("Customer bot Telegram identity or webhook check failed") from None


async def verify_resource_permissions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    bot: Bot,
) -> None:
    """Refuse sales if the customer bot cannot grant and revoke channel access."""
    async with system_project_session_scope(session_factory, project_id) as session:
        resources = list((await session.scalars(
            select(Resource).where(
                Resource.project_id == project_id,
                Resource.status == ResourceStatus.ACTIVE.value,
            )
        )).all())
    if not resources:
        raise RuntimeError("Customer bot project has no active channel/group")

    for resource in resources:
        try:
            member = await bot.get_chat_member(
                chat_id=resource.external_id,
                user_id=bot.id,
            )
            status = getattr(member.status, "value", member.status)
            if status != "creator" and (
                status != "administrator"
                or not member.can_invite_users
                or not member.can_restrict_members
            ):
                raise ValueError("Insufficient Telegram administrator rights")
        except Exception:
            # Do not print Telegram transport errors, which may contain secrets.
            raise RuntimeError(
                "Customer bot lacks required channel/group administrator rights"
            ) from None


async def run_customer_bot(settings: Settings | None = None) -> None:
    current = settings or get_settings()
    project_id = current.telegram_project_id
    bot_id = current.customer_telegram_bot_id
    if project_id is None or bot_id is None:
        raise RuntimeError(
            "TMS_TELEGRAM_PROJECT_ID and TMS_CUSTOMER_TELEGRAM_BOT_ID are required"
        )

    engine = build_engine(current)
    session_factory = build_session_factory(engine)
    bot: Bot | None = None
    worker: asyncio.Task[None] | None = None
    try:
        config = await load_customer_bot(
            session_factory,
            project_id=project_id,
            telegram_bot_id=bot_id,
            settings=current,
        )
        # The token remains confined to this process. It is never printed,
        # returned to Telegram UI or passed to external project contexts.
        try:
            bot = Bot(token=config.token)
        except Exception:
            # Token validation exceptions may contain credential material.
            raise RuntimeError("Customer bot credential is invalid") from None
        await check_customer_bot_identity(bot, config.telegram_bot_id)
        await verify_resource_permissions(
            session_factory,
            project_id=project_id,
            bot=bot,
        )

        dispatcher = create_customer_dispatcher(session_factory, project_id=project_id)
        worker = asyncio.create_task(
            run_project_runtime_loop(
                session_factory,
                project_id=project_id,
                adapter=TelegramAccessAdapter(bot),
                poll_seconds=current.telegram_worker_poll_seconds,
            )
        )
        logger.info("Customer bot runtime started for project %s", project_id)
        await dispatcher.start_polling(bot)
    finally:
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if bot is not None:
            await bot.session.close()
        await engine.dispose()
