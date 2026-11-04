"""Locale selection stays per user and does not infer location."""
from telegram_membership_saas import localization
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
        assert any(
            button.callback_data == "lng:menu"
            for row in keyboard.inline_keyboard for button in row
        )
        assert format_period(3, "month") == "3 months"
        assert format_period(None, "lifetime") == "Lifetime"
    finally:
        _language.reset(token)


def test_russian_buyer_fallback() -> None:
    text, keyboard = catalog_screen(())
    assert "Подписки" in text
    assert any(
            button.callback_data == "lng:menu"
            for row in keyboard.inline_keyboard for button in row
        )
    assert format_period(3, "month") == "3 месяца"



async def test_recipient_locale_overrides_buyer_and_restores(monkeypatch) -> None:
    async def read_language(_factory, telegram_id, telegram_hint):
        assert telegram_hint == "ru"
        return "en" if telegram_id == 200 else "ru"

    monkeypatch.setattr(localization, "read_language", read_language)
    token = _language.set("en")  # Buyer speaks English
    try:
        async with localization.recipient_language(object(), 100):
            assert current_language() == "ru"  # Reviewer #1 uses Russian
        assert current_language() == "en"  # Buyer scope is restored
        async with localization.recipient_language(object(), 200):
            assert current_language() == "en"  # Reviewer #2 chose English
        assert current_language() == "en"
    finally:
        _language.reset(token)
