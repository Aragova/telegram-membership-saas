"""Locale selection stays per user and does not infer location."""
from telegram_membership_saas.localization import (
    _language,
    current_language,
    telegram_language,
    tr,
)
from telegram_membership_saas.modules.telegram_commerce.service import format_period
from telegram_membership_saas.modules.telegram_commerce.storefront import catalog_screen


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


def test_english_buyer_empty_catalog_and_period() -> None:
    token = _language.set("en")
    try:
        text, keyboard = catalog_screen(())
        assert "Subscriptions" in text
        assert "No plans" in text
        assert keyboard.inline_keyboard[0][0].callback_data == "lng:menu"
        assert format_period(3, "month") == "3 months"
        assert format_period(None, "lifetime") == "Lifetime"
    finally:
        _language.reset(token)


def test_russian_buyer_fallback() -> None:
    text, keyboard = catalog_screen(())
    assert "Подписки" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "lng:menu"
    assert format_period(3, "month") == "3 месяца"
