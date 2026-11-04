from uuid import UUID

from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.modules.owner_setup.handlers import create_owner_setup_router
from telegram_membership_saas.modules.platform_dashboard_handlers import (
    create_platform_dashboard_router,
)
from telegram_membership_saas.modules.telegram_access import create_telegram_access_router
from telegram_membership_saas.modules.telegram_admin import create_telegram_admin_router
from telegram_membership_saas.modules.telegram_admin.redirects import create_central_cutover_router
from telegram_membership_saas.modules.telegram_commerce import create_telegram_commerce_router


def create_dispatcher(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    project_id: UUID | None = None,
    enable_access: bool = True,
) -> Dispatcher:
    dispatcher = Dispatcher()
    if session_factory is not None:
        if get_settings().platform_owner_telegram_id is not None:
            # Only the configured central control bot gets the platform panel;
            # connected buyer bots never include these routes.
            dispatcher.include_router(
                create_platform_dashboard_router(session_factory)
            )
        dispatcher.include_router(create_owner_setup_router(session_factory))
        if enable_access:
            dispatcher.include_router(create_telegram_access_router(session_factory))
        if project_id is not None:
            if enable_access:
                # Legacy single-project local mode only; never expose this
                # tenant administration router in cutover/central-only mode.
                dispatcher.include_router(
                    create_telegram_admin_router(
                        session_factory, project_id=project_id,
                    )
                )
                dispatcher.include_router(
                    create_telegram_commerce_router(
                        session_factory, project_id=project_id,
                    )
                )
            else:
                dispatcher.include_router(create_central_cutover_router())
    return dispatcher


def create_customer_dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
) -> Dispatcher:
    """Owner admin and buyer storefront in one project-bound BotInstance."""
    dispatcher = Dispatcher()
    # The tenant owner is recognized by database permissions on /start.
    # Buyers fall through to the same bot's storefront, never its admin UI.
    dispatcher.include_router(
        create_owner_setup_router(
            session_factory, fixed_project_id=project_id,
        )
    )
    dispatcher.include_router(
        create_telegram_access_router(session_factory, expected_project_id=project_id)
    )
    dispatcher.include_router(
        create_telegram_commerce_router(session_factory, project_id=project_id)
    )
    return dispatcher


def create_bot(settings: Settings | None = None) -> Bot:
    current = settings or get_settings()
    if current.telegram_bot_token is None:
        raise RuntimeError("TMS_TELEGRAM_BOT_TOKEN is required to start the Telegram bot")
    return Bot(token=current.telegram_bot_token.get_secret_value())
