"""Locale selection stays per user and does not infer location."""
from telegram_membership_saas.localization import (
    _language,
    current_language,
    telegram_language,
    tr,
)


def test_auto_language_detection() -> None:
    assert telegram_language("ru") == "ru"
    assert telegram_language("ru-RU") == "ru"
    assert telegram_language("RU_ru") == "ru"
    assert telegram_language("en-US") == "en"
    assert telegram_language("de-DE") == "en"
    assert telegram_language(None) == "en"


def test_language_is_scoped() -> None:
    assert current_language() == "ru"
    token = _language.set("en")
    try:
        assert tr("Настройки", "Settings") == "Settings"
    finally:
        _language.reset(token)
    assert tr("Настройки", "Settings") == "Настройки"
