import pytest
from pydantic import ValidationError

from telegram_membership_saas.config import Settings


def test_default_project_timezone_is_moscow() -> None:
    settings = Settings(_env_file=None)
    assert settings.default_project_timezone == "Europe/Moscow"


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(default_project_timezone="Mars/Olympus", _env_file=None)
