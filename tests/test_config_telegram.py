from uuid import uuid4

from telegram_membership_saas.config import Settings


def test_empty_telegram_project_id_is_optional(monkeypatch) -> None:
    monkeypatch.setenv("TMS_TELEGRAM_PROJECT_ID", "")

    settings = Settings(_env_file=None)

    assert settings.telegram_project_id is None


def test_telegram_project_id_parses_uuid(monkeypatch) -> None:
    project_id = uuid4()
    monkeypatch.setenv("TMS_TELEGRAM_PROJECT_ID", str(project_id))

    settings = Settings(_env_file=None)

    assert settings.telegram_project_id == project_id
