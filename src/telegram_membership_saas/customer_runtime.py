"""Single-project customer-facing BotInstance polling runtime.

The first safe step toward multi-bot: one customer bot per project, in a
separate process from central ViMi Pay. Never run several access workers for
the same project's resources until actions are routed by BotInstance.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from aiogram import Bot
from aiogram.methods import GetUpdates
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
from telegram_membership_saas.modules.broadcasts.worker import run_broadcast_loop
from telegram_membership_saas.modules.projects import Project, ProjectStatus
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.telegram_access import TelegramAccessAdapter
from telegram_membership_saas.project_worker_lock import project_worker_lock
from telegram_membership_saas.rich_messages import RichUiBot
from telegram_membership_saas.task_lifecycle import cancel_once, complete_cleanup
from telegram_membership_saas.telegram_adapter import create_customer_dispatcher
from telegram_membership_saas.telegram_menus import install_public_commands
from telegram_membership_saas.telegram_runtime import run_project_runtime_loop

logger = logging.getLogger(__name__)


class ObservedCustomerBot(RichUiBot):
    """Report only successful getUpdates responses, even when there are no updates."""

    def __init__(
        self, *, token: str, on_poll_response: Callable[[], None] | None = None
    ) -> None:
        super().__init__(token=token)
        self._poll_response = on_poll_response

    async def __call__(self, method, request_timeout=None):
        result = await super().__call__(method, request_timeout=request_timeout)
        if isinstance(method, GetUpdates) and self._poll_response is not None:
            self._poll_response()
        return result


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
        # Still run the entitlement worker: subscriptions issued for a
        # previously disabled channel may require expiry/revocation.
        return

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


async def run_customer_bot_instance(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    telegram_bot_id: int,
    settings: Settings,
    on_started: Callable[[], Awaitable[None]] | None = None,
    on_poll_response: Callable[[], None] | None = None,
) -> None:
    """Start exactly one verified buyer-only poller within an acquired project lease."""
    bot: Bot | None = None
    worker: asyncio.Task[None] | None = None
    broadcast_worker: asyncio.Task[None] | None = None
    polling: asyncio.Task[None] | None = None
    dispatcher = None
    try:
        config = await load_customer_bot(
            session_factory,
            project_id=project_id,
            telegram_bot_id=telegram_bot_id,
            settings=settings,
        )
        try:
            bot = ObservedCustomerBot(
                token=config.token, on_poll_response=on_poll_response
            )
        except Exception:
            raise RuntimeError("Customer bot credential is invalid") from None

        await check_customer_bot_identity(bot, config.telegram_bot_id)
        await verify_resource_permissions(
            session_factory, project_id=project_id, bot=bot
        )
        dispatcher = create_customer_dispatcher(session_factory, project_id=project_id)
        await install_public_commands(bot, customer=True)
        worker = asyncio.create_task(
            run_project_runtime_loop(
                session_factory,
                project_id=project_id,
                adapter=TelegramAccessAdapter(bot, session_factory=session_factory),
                poll_seconds=settings.telegram_worker_poll_seconds,
            )
        )
        broadcast_worker = asyncio.create_task(
            run_broadcast_loop(
                session_factory, project_id=project_id,
                telegram_bot_id=telegram_bot_id, bot=bot,
            )
        )
        if on_started is not None:
            await on_started()
        logger.info("Customer bot ready for project %s", project_id)
        # The supervisor can run many independent dispatchers inside one
        # process; only the top-level app should own OS signals.
        polling = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
                handle_as_tasks=False,
            )
        )
        # Never propagate cancellation to aiogram's start_polling task directly.
        # It manages an internal GetUpdates task that must be stopped gracefully.
        await asyncio.shield(polling)
    finally:
        async def shutdown() -> None:
            try:
                if polling is not None:
                    if not polling.done() and dispatcher is not None:
                        # Stop aiogram's internally managed GetUpdates reader
                        # gracefully; cancellation could otherwise orphan it.
                        await asyncio.sleep(0)
                        try:
                            if not polling.done():
                                await dispatcher.stop_polling()
                        except RuntimeError:
                            cancel_once(polling)
                        except Exception:
                            cancel_once(polling)
                    await asyncio.gather(polling, return_exceptions=True)
            finally:
                # Access maintenance must also finish before the caller is
                # permitted to release its per-project advisory lock.
                try:
                    if worker is not None:
                        cancel_once(worker)
                        await asyncio.gather(worker, return_exceptions=True)
                finally:
                    if broadcast_worker is not None:
                        cancel_once(broadcast_worker)
                        await asyncio.gather(
                            broadcast_worker, return_exceptions=True,
                        )
                    if bot is not None:
                        await bot.session.close()

        await complete_cleanup(shutdown())


async def run_customer_bot(settings: Settings | None = None) -> None:
    """Legacy one-bot command remains protected by the same project lease."""
    from telegram_membership_saas.project_worker_lock import (
        confirm_project_lock,
    )

    current = settings or get_settings()
    project_id = current.telegram_project_id
    bot_id = current.customer_telegram_bot_id
    if project_id is None or bot_id is None:
        raise RuntimeError(
            "TMS_TELEGRAM_PROJECT_ID and TMS_CUSTOMER_TELEGRAM_BOT_ID are required"
        )

    engine = build_engine(current)
    factory = build_session_factory(engine)
    try:
        async with project_worker_lock(engine, project_id) as lease:
            polling = asyncio.create_task(
                run_customer_bot_instance(
                    factory, project_id=project_id,
                    telegram_bot_id=bot_id, settings=current,
                )
            )

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(3)
                    await confirm_project_lock(lease, project_id)

            watchdog = asyncio.create_task(heartbeat())
            try:
                done, _ = await asyncio.wait(
                    (polling, watchdog), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
            finally:
                async def shutdown() -> None:
                    cancel_once(polling)
                    cancel_once(watchdog)
                    await asyncio.gather(
                        polling, watchdog, return_exceptions=True
                    )

                await complete_cleanup(shutdown())
    finally:
        await engine.dispose()
