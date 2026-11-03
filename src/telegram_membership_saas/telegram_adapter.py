from aiogram import Bot, Dispatcher

from telegram_membership_saas.config import Settings, get_settings


def create_dispatcher() -> Dispatcher:
    return Dispatcher()


def create_bot(settings: Settings | None = None) -> Bot:
    current = settings or get_settings()
    if current.telegram_bot_token is None:
        raise RuntimeError("TMS_TELEGRAM_BOT_TOKEN is required to start the Telegram bot")
    return Bot(token=current.telegram_bot_token.get_secret_value())
