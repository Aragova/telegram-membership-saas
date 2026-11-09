"""Rich file checkout must preserve production callbacks and legacy compatibility."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas import localization
from telegram_membership_saas.modules.file_sales import rich_ui


def _bot(bot_id=1234):
    bot = MagicMock()
    bot.id = bot_id
    bot.send_message = AsyncMock()
    bot.send_rich_message = AsyncMock()
    bot.send_invoice = AsyncMock()
    return bot


def _keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Pay", callback_data="fm:method:some-id"),
    ]])


@pytest.mark.parametrize(
    ("locale", "heading", "price_label", "info_label"),
    [
        ("ru", "Покупка файла", "Цена", "Описание"),
        ("en", "File purchase", "Price", "Description"),
    ],
)
def test_rich_card_escapes_user_content(locale, heading, price_label, info_label):
    token = localization._language.set(locale)
    try:
        html = rich_ui.rich_file_card_html(
            product_name='Video <script>alert("x")</script> & more',
            description="One <two> & three\nSecond paragraph",
            price="100 RUB", file_count=2,
        )
    finally:
        localization._language.reset(token)
    assert "<h3>" in html and heading in html
    assert "<table bordered compact>" in html
    assert price_label in html and "<td>100 RUB</td>" in html
    assert "<details>" in html and info_label in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;two&gt; &amp; three" in html
    assert html.count("<p>") >= 3
    assert "<p>📦 <b>" not in html


@pytest.mark.parametrize(
    ("locale", "name_label"),
    [
        ("ru", "🏷 Название"),
        ("en", "🏷 Name"),
    ],
)
def test_product_name_is_first_rich_table_row_in_both_languages(
    locale, name_label,
):
    token = localization._language.set(locale)
    try:
        html = rich_ui.rich_file_card_html(
            product_name="Моя <жопа> & видео",
            description="Test", price="100 RUB",
            file_count=1,
        )
    finally:
        localization._language.reset(token)
    start = html.index("<table bordered compact>")
    first = (
        "<tr><th>" + name_label
        + "</th><td>Моя &lt;жопа&gt; &amp; видео</td></tr>"
    )
    second = "<tr><th>" + ("💰 Цена" if locale == "ru" else "💰 Price")
    third = "<tr><th>" + ("📎 Файлов" if locale == "ru" else "📎 Files")
    assert html.index(first) > start
    assert html.index(first) < html.index(second) < html.index(third)
    assert html.count("Моя &lt;жопа&gt; &amp; видео") == 1
    assert "<p>📦 <b>" not in html
    assert "<td>100 RUB</td>" in html


async def test_default_disabled_keeps_exact_legacy_checkout(monkeypatch):
    monkeypatch.setattr(
        rich_ui, "get_settings",
        lambda: SimpleNamespace(rich_file_checkout_bot_ids=""),
    )
    bot = _bot()
    keys = _keyboard()
    used = await rich_ui.send_file_card(
        bot, chat_id=1234,
        rich_html="<h2>New</h2>", legacy_html="<b>Original</b>",
        reply_markup=keys,
    )
    assert not used
    bot.send_rich_message.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        1234, "<b>Original</b>", parse_mode="HTML", reply_markup=keys,
    )


async def test_enabled_bot_sends_rich_message_with_original_callbacks(monkeypatch):
    monkeypatch.setattr(
        rich_ui, "get_settings",
        lambda: SimpleNamespace(rich_file_checkout_bot_ids="1234, 6789"),
    )
    bot = _bot()
    keys = _keyboard()
    used = await rich_ui.send_file_card(
        bot, chat_id=456, rich_html="<h3>New</h3>",
        legacy_html="<b>Original</b>", reply_markup=keys,
    )
    assert used
    bot.send_message.assert_not_awaited()
    args = bot.send_rich_message.await_args.kwargs
    assert args["chat_id"] == 456
    assert args["rich_message"].html == "<h3>New</h3>"
    assert args["reply_markup"] is keys
    assert args["reply_markup"].inline_keyboard[0][0].callback_data == (
        "fm:method:some-id"
    )
    other = _bot(9999)
    await rich_ui.send_file_card(
        other, chat_id=456, rich_html="<h3>New</h3>",
        legacy_html="<b>Original</b>", reply_markup=keys,
    )
    other.send_rich_message.assert_not_awaited()


async def test_rich_method_bad_request_falls_back_once(monkeypatch):
    monkeypatch.setattr(
        rich_ui, "get_settings",
        lambda: SimpleNamespace(rich_file_checkout_bot_ids="1234"),
    )
    bot = _bot()
    bot.send_rich_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=1234, text="dummy"),
        message="Bad Request: can't parse Rich Message",
    )
    keys = _keyboard()
    used = await rich_ui.send_file_card(
        bot, chat_id=1234, rich_html="<h3>Rich</h3>",
        legacy_html="<b>Legacy</b>", reply_markup=keys,
    )
    assert not used
    bot.send_rich_message.assert_awaited_once()
    bot.send_message.assert_awaited_once()
