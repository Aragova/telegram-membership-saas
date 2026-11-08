"""Opt-in Rich Messages preview must not change real checkout or callbacks."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas import localization
from telegram_membership_saas.modules.file_sales import handlers as admin_ui
from telegram_membership_saas.modules.file_sales import rich_ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


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
    assert html.count("<p>") >= 4


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


async def test_owner_preview_is_read_only_and_restricted(monkeypatch):
    project, product, folder, owner = uuid4(), uuid4(), uuid4(), uuid4()
    router = admin_ui.create_file_sales_router(MagicMock(), project_id=project)
    handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "file_callback"
    )
    monkeypatch.setattr(admin_ui, "ensure_telegram_actor",
                        AsyncMock(return_value=owner))
    owner_allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(admin_ui, "can_manage_project", owner_allowed)
    monkeypatch.setattr(admin_ui, "view_product", AsyncMock(return_value=(
        SimpleNamespace(
            id=product, folder_id=folder, name="Video",
            description="Demo", stars_price=500, status="published",
        ), 1,
    )))
    monkeypatch.setattr(admin_ui, "price_editor_options", AsyncMock(
        return_value=(("RUB", Decimal("100.000000000000000000")),),
    ))
    preview = AsyncMock(return_value=True)
    monkeypatch.setattr(admin_ui, "send_file_card", preview)
    bot = _bot()
    query = SimpleNamespace(
        data="fs:richpreview:" + encode_uuid(product),
        from_user=SimpleNamespace(
            id=1234, username="owner", first_name="Owner",
        ),
        message=SimpleNamespace(chat=SimpleNamespace(
            type="private", id=1234,
        )),
        answer=AsyncMock(),
    )
    await handler(query, bot)
    preview.assert_awaited_once()
    assert preview.await_args.kwargs["preview"] is True
    assert preview.await_args.kwargs["rich_html"].count("100 RUB") == 1
    assert "100.000000000000000000" not in preview.await_args.kwargs["rich_html"]
    assert "100 RUB" in preview.await_args.kwargs["legacy_html"]
    assert "Предпросмотр оформления товара" in preview.await_args.kwargs["rich_html"]
    # The owner fallback must display real line breaks, not a literal backslash-n.
    legacy = preview.await_args.kwargs["legacy_html"]
    assert "\n" in legacy
    assert "\\n" not in legacy
    keys = preview.await_args.kwargs["reply_markup"].inline_keyboard
    assert [button.callback_data for row in keys for button in row] == [
        "fs:ap:" + encode_uuid(product),
    ]
    bot.send_invoice.assert_not_awaited()
    owner_allowed.return_value = False
    await handler(query, bot)
    preview.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("100.000000000000000000"), "100"),
        (Decimal("100.250000000000000000"), "100.25"),
        (Decimal("0.010000000000000000"), "0.01"),
        (Decimal("0.000000000000000001"), "0.000000000000000001"),
        (Decimal("0E-18"), "0"),
        (Decimal("1000"), "1000"),
    ],
)
def test_rich_preview_amount_drops_only_insignificant_zeros(amount, expected):
    assert admin_ui._compact_preview_price(amount) == expected


@pytest.mark.parametrize(
    ("locale", "preview_prompt", "checkout_prompt"),
    [
        ("ru", "✨ Предпросмотр оформления товара", "👇 Выберите способ оплаты ниже"),
        ("en", "✨ Product design preview", "👇 Select a payment method below"),
    ],
)
def test_rich_preview_caption_is_distinct_from_real_checkout(
    locale, preview_prompt, checkout_prompt,
):
    token = localization._language.set(locale)
    try:
        data = dict(
            product_name="Video", description="Demo",
            price="100 RUB", file_count=1,
        )
        preview_html = rich_ui.rich_file_card_html(**data, preview=True)
        checkout_html = rich_ui.rich_file_card_html(**data)
    finally:
        localization._language.reset(token)
    assert preview_prompt in preview_html
    assert checkout_prompt not in preview_html
    assert checkout_prompt in checkout_html
    assert preview_prompt not in checkout_html
