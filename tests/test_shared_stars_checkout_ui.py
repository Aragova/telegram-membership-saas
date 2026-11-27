"""Shared buyer Stars screen: locale, actual Rich table, navigation fallback."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup

from telegram_membership_saas.localization import _language
from telegram_membership_saas.rich_messages import to_rich_html
from telegram_membership_saas.stars_checkout_ui import (
    buyer_stars_checkout_html,
    log_stars_checkout_failure,
    send_stars_screen,
    stars_topup_button,
)


@pytest.mark.parametrize("language,title", [
    ("ru", "⭐ Оплата покупки"),
    ("en", "⭐ Purchase payment"),
])
def test_buyer_stars_details_render_as_safe_rich_table(language, title):
    token = _language.set(language)
    try:
        text = buyer_stars_checkout_html(
            title=title, details=[("📦 Product", "<b>& test")], amount=500,
        )
        button = stars_topup_button()
    finally:
        _language.reset(token)
    rich = to_rich_html(text)
    assert "<table bordered compact>" in rich
    assert "&lt;b&gt;&amp; test" in rich
    assert "500 ⭐" in rich
    assert "@PremiumBot" in rich
    assert button.url == "https://t.me/PremiumBot"


@pytest.mark.parametrize("error, fallback", [
    ("message is not modified", False),
    ("message to edit not found", True),
])
async def test_stars_screen_only_falls_back_for_uneditable_message(error, fallback):
    method = EditMessageText(chat_id=123, message_id=77, text="old")
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(method, error))
    bot.send_message = AsyncMock()
    await send_stars_screen(
        bot, chat_id=123, html="new", message_id=77,
        keyboard=InlineKeyboardMarkup(inline_keyboard=[[stars_topup_button()]]),
    )
    assert bot.send_message.await_count == int(fallback)


async def test_stars_screen_does_not_retry_ambiguous_transport_failure():
    method = EditMessageText(chat_id=123, message_id=77, text="old")
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramNetworkError(method, "timeout"))
    bot.send_message = AsyncMock()
    with pytest.raises(TelegramNetworkError):
        await send_stars_screen(
            bot, chat_id=123, html="new", message_id=77,
            keyboard=InlineKeyboardMarkup(inline_keyboard=[[stars_topup_button()]]),
        )
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("state, expected", [("42501", "42501"), ("SECRET", "-"), (None, "-")])
def test_checkout_failure_logs_stack_without_payment_secrets(caplog, state, expected):
    logger = logging.getLogger("test.stars")
    try:
        error = RuntimeError("SECRET_TOKEN_AND_PAYMENT_PAYLOAD")
        error.orig = SimpleNamespace(sqlstate=state)
        raise error
    except RuntimeError as exc:
        log_stars_checkout_failure(logger, "Stars checkout", exc)
    assert "RuntimeError" in caplog.text
    assert "SQLSTATE=" + expected in caplog.text
    assert "test_checkout_failure_logs_stack_without_payment_secrets" in caplog.text
    assert "SECRET" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
