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
from functools import partial
from random import uniform
from uuid import UUID

from aiogram import Bot
from aiogram.methods import GetUpdates
from aiogram.methods.base import TelegramMethod
from aiogram.utils.backoff import BackoffConfig
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
from telegram_membership_saas.modules.bots.diagnostics import (
    CustomerBotDiagnosticError,
    telegram_stage_code,
    telegram_stage_error,
)
from telegram_membership_saas.modules.bots.service import decrypt_token
from telegram_membership_saas.modules.bots.telegram_preflight_retry import (
    retry_telegram_preflight,
)
from telegram_membership_saas.modules.broadcasts.worker import run_broadcast_loop
from telegram_membership_saas.modules.crypto_pay.recovery import run_crypto_pay_recovery_loop
from telegram_membership_saas.modules.projects import Project, ProjectStatus
from telegram_membership_saas.modules.resources import Resource, ResourceStatus
from telegram_membership_saas.modules.stars_subscription_recovery import (
    run_subscription_stars_ledger_loop,
)
from telegram_membership_saas.modules.telegram_access import TelegramAccessAdapter
from telegram_membership_saas.project_worker_lock import project_worker_lock
from telegram_membership_saas.rich_messages import RichUiBot
from telegram_membership_saas.task_lifecycle import (
    cancel_and_drain,
    cancel_once,
    complete_cleanup,
    wait_for_runtime_tasks,
)
from telegram_membership_saas.telegram_adapter import create_customer_dispatcher
from telegram_membership_saas.telegram_menus import (
    install_public_commands,
    sync_project_owner_commands,
)
from telegram_membership_saas.telegram_runtime import run_project_runtime_loop

logger = logging.getLogger(__name__)
CUSTOMER_COMMAND_INSTALL_TIMEOUT_SECONDS = 12.0
# Shared VM may poll 50+ independent Telegram bots. Avoid a synchronized
# getUpdates retry storm during a Telegram-side 502/429 wave while still
# recovering far faster than a full worker teardown and startup.
TELEGRAM_POLL_BACKOFF = BackoffConfig(
    min_delay=1.0, max_delay=20.0, factor=1.6, jitter=0.3,
)

# Only network, server 5xx and rate limits are safe for a long-lived
# preflight retry. Invalid tokens, wrong Bot ID, webhook conflicts and lost
# channel permissions ALWAYS fail closed and release the lease.
TRANSIENT_PREFLIGHT_CODES = frozenset(
    f"{stage}_{kind}"
    for stage in ("identity", "webhook", "resource")
    for kind in ("network_failed", "server_failed", "rate_limited")
)


async def retry_customer_preflight(
    operation: Callable[[], Awaitable[None]],
    *, on_failure: Callable[[str, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Keep the same bot/lease alive across Telegram transport outages.

    A new worker is not launched for each 502, so it does not repeat the
    entire setup and synchronized getMe handshake on every retry. The
    existing per-call bounded retry and timeouts remain in effect.
    """
    attempt = 0
    while True:
        try:
            await operation()
            return
        except CustomerBotDiagnosticError as exc:
            if exc.code not in TRANSIENT_PREFLIGHT_CODES:
                raise
            # Never lose Telegram's explicit provider deadline. The
            # supervisor receives the planned numeric cooldown as well, so
            # its startup watchdog cannot cancel a required 300s wait.
            base = min(60.0, 2.0 * 2 ** min(attempt, 5))
            delay = (
                max(base, exc.retry_after if exc.retry_after is not None else 60.0)
                if exc.code.endswith("_rate_limited") else base
            )
            pause = delay + uniform(0.0, min(4.0, base * 0.2))
            if on_failure is not None:
                on_failure(exc.code, pause)
            await sleep(pause)
            attempt += 1


class ObservedCustomerBot(RichUiBot):
    """Report only successful getUpdates responses, even when there are no updates."""

    def __init__(
        self, *, token: str,
        on_poll_response: Callable[[], None] | None = None,
        on_poll_failure: Callable[[str], None] | None = None,
        on_poll_request: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(token=token)
        self._poll_response = on_poll_response
        self._poll_failure = on_poll_failure
        self._poll_request = on_poll_request

    async def __call__[T](
        self, method: TelegramMethod[T], request_timeout: int | None = None,
    ) -> T:
        # An in-flight request is not proof of a successful Telegram response.
        # This milestone also excludes all preflight work from polling timeout.
        if isinstance(method, GetUpdates) and self._poll_request is not None:
            self._poll_request()
        try:
            result = await super().__call__(method, request_timeout=request_timeout)
        except Exception as exc:
            if isinstance(method, GetUpdates) and self._poll_failure is not None:
                # Only a fixed safe code leaves this method. Aiogram keeps
                # its existing retry behavior; do NOT swallow the error.
                self._poll_failure(telegram_stage_code("polling", exc))
            raise
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


async def check_customer_bot_identity(
    bot: Bot, telegram_bot_id: int, *,
    on_startup_stage: Callable[[str], None] | None = None,
) -> None:
    """Fail closed at the EXACT stage while preserving token secrecy."""
    if on_startup_stage is not None:
        on_startup_stage("identity")
    try:
        # aiogram's Bot.me() caches the verified result. start_polling()
        # requests it again, so caching avoids a second getMe handshake.
        actual = await retry_telegram_preflight(bot.me)
    except Exception as exc:
        raise telegram_stage_error("identity", exc) from None
    if actual.id != telegram_bot_id or not actual.is_bot:
        raise CustomerBotDiagnosticError("identity_mismatch")

    if on_startup_stage is not None:
        on_startup_stage("webhook")
    try:
        webhook = await retry_telegram_preflight(bot.get_webhook_info)
    except Exception as exc:
        raise telegram_stage_error("webhook", exc) from None
    if webhook.url:
        # Never expose the webhook URL to Telegram UI or runtime logs.
        raise CustomerBotDiagnosticError("webhook_active")


async def verify_resource_permissions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    bot: Bot,
    on_startup_stage: Callable[[str], None] | None = None,
) -> None:
    """Refuse sales if the customer bot cannot grant and revoke channel access."""
    if on_startup_stage is not None:
        on_startup_stage("resource")
    async with system_project_session_scope(session_factory, project_id) as session:
        resources = list((await session.scalars(
            select(Resource).where(
                Resource.project_id == project_id,
                Resource.status == ResourceStatus.ACTIVE.value,
                # A removed channel deliberately stays ACTIVE in storage so
                # existing invoices and entitlement reconciliation survive.
                # It must not veto startup of the remaining customer bot.
                Resource.removed_at.is_(None),
            )
        )).all())
    if not resources:
        # Still run the entitlement worker: subscriptions issued for a
        # previously disabled channel may require expiry/revocation.
        return

    for resource in resources:
        try:
            member = await retry_telegram_preflight(
                partial(bot.get_chat_member, chat_id=resource.external_id, user_id=bot.id),
            )
        except Exception as exc:
            # A transient getChatMember failure is NEVER evidence that the
            # administrator rights were revoked. On repeated failure, fail
            # closed before starting buyer polling or granting new access.
            # The next supervisor attempt rechecks the original rights.
            raise telegram_stage_error("resource", exc) from None
        status = getattr(member.status, "value", member.status)
        if status != "creator" and (
            status != "administrator"
            or not getattr(member, "can_invite_users", False)
            or not getattr(member, "can_restrict_members", False)
        ):
            raise CustomerBotDiagnosticError("missing_admin_rights")


async def run_customer_bot_instance(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    telegram_bot_id: int,
    settings: Settings,
    on_started: Callable[[], Awaitable[None]] | None = None,
    on_poll_response: Callable[[], None] | None = None,
    on_poll_failure: Callable[[str], None] | None = None,
    on_poll_request: Callable[[], None] | None = None,
    on_startup_stage: Callable[[str], None] | None = None,
    on_preflight_failure: Callable[[str, float], None] | None = None,
) -> None:
    """Start exactly one verified buyer-only poller within an acquired project lease."""
    bot: Bot | None = None
    worker: asyncio.Task[None] | None = None
    broadcast_worker: asyncio.Task[None] | None = None
    stars_reconciliation: asyncio.Task[None] | None = None
    crypto_reconciliation: asyncio.Task[None] | None = None
    polling: asyncio.Task[None] | None = None
    dispatcher = None
    try:
        if on_startup_stage is not None:
            on_startup_stage("initializing")
        config = await load_customer_bot(
            session_factory,
            project_id=project_id,
            telegram_bot_id=telegram_bot_id,
            settings=settings,
        )
        try:
            bot = ObservedCustomerBot(
                token=config.token, on_poll_response=on_poll_response,
                on_poll_failure=on_poll_failure,
                on_poll_request=on_poll_request,
            )
        except Exception:
            raise RuntimeError("Customer bot credential is invalid") from None

        await retry_customer_preflight(
            lambda: check_customer_bot_identity(
                bot, config.telegram_bot_id, on_startup_stage=on_startup_stage,
            ),
            on_failure=on_preflight_failure,
        )
        await retry_customer_preflight(
            lambda: verify_resource_permissions(
                session_factory, project_id=project_id, bot=bot,
                on_startup_stage=on_startup_stage,
            ),
            on_failure=on_preflight_failure,
        )
        dispatcher = create_customer_dispatcher(session_factory, project_id=project_id)
        if on_startup_stage is not None:
            on_startup_stage("commands")
        try:
            # A best-effort native menu must never block the buyer poller.
            await asyncio.wait_for(
                install_public_commands(bot, customer=True),
                timeout=CUSTOMER_COMMAND_INSTALL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("Customer native command menu setup timed out")
        # A deployed update must fix previously opened owner chats, not only
        # owners who happen to send /start again. This is best-effort and
        # cannot block the customer bot's purchases or access worker.
        try:
            await asyncio.wait_for(
                sync_project_owner_commands(
                    bot, session_factory, project_id=project_id,
                ),
                timeout=CUSTOMER_COMMAND_INSTALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("Could not synchronize connected owner commands (%s)",
                           type(exc).__name__)
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
        stars_reconciliation = asyncio.create_task(
            run_subscription_stars_ledger_loop(
                session_factory, bot=bot, project_id=project_id,
            )
        )
        if settings.bot_token_encryption_key is not None:
            crypto_reconciliation = asyncio.create_task(
                run_crypto_pay_recovery_loop(
                    session_factory, project_id=project_id, bot=bot,
                    encryption_key=settings.bot_token_encryption_key,
                )
            )
        if on_started is not None:
            await on_started()
        logger.info("Customer bot ready for project %s", project_id)
        if on_startup_stage is not None:
            on_startup_stage("polling")
        # The supervisor can run many independent dispatchers inside one
        # process; only the top-level app should own OS signals.
        polling = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
                handle_as_tasks=False,
                backoff_config=TELEGRAM_POLL_BACKOFF,
            )
        )
        # asyncio.wait observes completion without propagating cancellation
        # to aiogram or sibling tasks. Cleanup still owns the shutdown order.
        background = {
            "access": worker,
            "broadcast": broadcast_worker,
            "stars_recovery": stars_reconciliation,
        }
        if crypto_reconciliation is not None:
            background["crypto_recovery"] = crypto_reconciliation
        await wait_for_runtime_tasks(polling, background)
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
                    await cancel_and_drain(
                        worker, broadcast_worker, stars_reconciliation,
                        crypto_reconciliation,
                    )
                finally:
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
