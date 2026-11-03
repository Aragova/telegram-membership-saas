import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.telegram_adapter import create_bot, create_dispatcher


def test_dispatcher_can_be_created_without_secret() -> None:
    assert create_dispatcher() is not None


def test_bot_requires_runtime_token() -> None:
    settings = Settings(telegram_bot_token=None, _env_file=None)
    with pytest.raises(RuntimeError, match="TMS_TELEGRAM_BOT_TOKEN"):
        create_bot(settings)
