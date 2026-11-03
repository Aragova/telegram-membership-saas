from uuid import UUID

from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import Settings, get_settings
from telegram_membership_saas.modules.telegram_access import create_telegram_access_router
from telegram_membership_saas.modules.telegram_commerce import create_telegram_commerce_router


def create_dispatcher(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    project_id: UUID | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()
    if session_factory is not None:
        dispatcher.include_router(create_telegram_access_router(session_factory))
        if project_id is not None:
            dispatcher.include_router(
                create_telegram_commerce_router(
                    session_factory,
                    project_id=project_id,
                )
            )
    return dispatcher


def create_bot(settings: Settings | None = None) -> Bot:
    current = settings or get_settings()
    if current.telegram_bot_token is None:
        raise RuntimeError("TMS_TELEGRAM_BOT_TOKEN is required to start the Telegram bot")
    return Bot(token=current.telegram_bot_token.get_secret_value())
