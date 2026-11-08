"""Shared buyer manual-checkout visuals: no changes to payment or receipt state."""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas import localization
from telegram_membership_saas import manual_checkout_ui as ui
from telegram_membership_saas.modules.file_sales import manual_handlers as file_ui
from telegram_membership_saas.modules.telegram_commerce import handlers as plan_ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _keys():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Paid", callback_data="paid:one")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel:one", style="danger")],
    ])


def _bot():
    bot = MagicMock()
    bot.id = 777
    bot.send_message = AsyncMock()
    bot.send_rich_message = AsyncMock()
    return bot


@pytest.mark.parametrize(("language", "expected"), [
    ("ru", ("Оформление покупки файла", "Название", "Цена", "Способ")),
    ("en", ("File purchase checkout", "Name", "Price", "Method")),
])
def test_file_checkout_rich_card_escapes_and_preserves_instructions(language, expected):
    original = "Оплатите 100 RUB на карту\n\n2204240225046459\nЗаметка <ваше> & наша"
    token = localization._language.set(language)
    try:
        html = ui.manual_checkout_rich_html(
            kind="file", name="Моя <жопа> & видео", period=None,
            amount="100", currency="RUB", payment_method="Ozon & Банк",
            instruction=original, guidance_html="Attach receipt.",
        )
    finally:
        localization._language.reset(token)
    for label in expected:
        assert label in html
    assert html.count("<tr>") == 3
    assert "Моя &lt;жопа&gt; &amp; видео" in html
    assert "Ozon &amp; Банк" in html
    assert "Оплатите 100 RUB на карту<br><br>2204240225046459" in html
    assert "Заметка &lt;ваше&gt; &amp; наша" in html
    assert "<ваше>" not in html


@pytest.mark.parametrize(("language", "expected"), [
    ("ru", ("Оформление подписки", "Тариф", "Срок", "Цена", "Способ")),
    ("en", ("Subscription checkout", "Plan", "Duration", "Price", "Method")),
])
def test_subscription_checkout_rich_row_order_and_optional_plan(language, expected):
    token = localization._language.set(language)
    try:
        html = ui.manual_checkout_rich_html(
            kind="subscription", name="Premium <VIP>", period="1 month",
            amount="550", currency="RUB", payment_method="Ozon Банк",
            instruction="СБП: +7 999 000-00-00", guidance_html="Receipt please",
        )
        unnamed = ui.manual_checkout_rich_html(
            kind="subscription", name=None, period="1 month",
            amount="550", currency="RUB", payment_method="Ozon Банк",
            instruction="2204240225046459", guidance_html="Receipt please",
        )
    finally:
        localization._language.reset(token)
    assert all(term in html for term in expected)
    assert html.count("<tr>") == 4
    assert html.index("<th>🎟") < html.index("<th>⏳") < html.index("<th>💰")
    assert "Premium &lt;VIP&gt;" in html
    assert unnamed.count("<tr>") == 3 and "<th>🎟" not in unnamed


@pytest.mark.parametrize(("instruction", "expected"), [
    ("Оплатите 100 RUB\n\n2204240225046459", "2204240225046459"),
    ("Карта: 2204 2402 2504 6459", "2204 2402 2504 6459"),
    ("СБП: +7 (999) 123-45-67", "+7 (999) 123-45-67"),
    ("Card: 2204240225046459", "2204240225046459"),
    ("2204240225046459\n1111222233334444", None),
    ("Перевести 100 RUB через QR", None),
    ("Номер: 12345", None),
    ("+79991234567", None),
])
def test_copy_only_unambiguous_original_payment_detail(instruction, expected):
    assert ui.copyable_payment_detail(instruction) == expected


def test_native_copy_button_preserves_original_payment_callbacks():
    original = _keys()
    rich = ui.copyable_checkout_keyboard(
        original, "На карту:\n2204240225046459",
    )
    assert rich.inline_keyboard[0][0].copy_text.text == "2204240225046459"
    assert [b.callback_data for row in rich.inline_keyboard[1:] for b in row] == [
        "paid:one", "cancel:one",
    ]
    assert rich.inline_keyboard[-1][0].style == "danger"
    assert len(original.inline_keyboard) == 2
    assert ui.copyable_checkout_keyboard(original, "Сумма 100 RUB") is original


async def test_rich_send_includes_native_copy_and_never_resends(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    bot = _bot()
    keys = _keys()
    await ui.send_manual_checkout(
        bot, chat_id=100, rich_html="<h3>Rich</h3>",
        legacy_html="<b>Original</b>", instruction="2204240225046459",
        reply_markup=keys,
    )
    bot.send_message.assert_not_awaited()
    rich_args = bot.send_rich_message.await_args.kwargs
    assert rich_args["rich_message"].html == "<h3>Rich</h3>"
    assert rich_args["reply_markup"].inline_keyboard[0][0].copy_text.text == (
        "2204240225046459"
    )
    assert rich_args["reply_markup"].inline_keyboard[1:] == keys.inline_keyboard
    bot.send_rich_message.reset_mock()
    bot.send_rich_message.side_effect = ConnectionError("timeout after send")
    with pytest.raises(ConnectionError):
        await ui.send_manual_checkout(
            bot, chat_id=100, rich_html="<h3>Rich</h3>",
            legacy_html="<b>Original</b>", instruction="2204240225046459",
            reply_markup=keys,
        )
    bot.send_message.assert_not_awaited()


async def test_api_rich_rejection_uses_unmodified_classic_once(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    bot = _bot()
    bot.send_rich_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=100, text="original"),
        message="Bad Request: can't parse Rich HTML",
    )
    keys = _keys()
    await ui.send_manual_checkout(
        bot, chat_id=100, rich_html="<h3>Rich</h3>",
        legacy_html="Оплатить <b>100 RUB</b>\n2204240225046459",
        instruction="2204240225046459", reply_markup=keys,
    )
    bot.send_rich_message.assert_awaited_once()
    bot.send_message.assert_awaited_once_with(
        100, "Оплатить <b>100 RUB</b>\n2204240225046459",
        parse_mode="HTML", reply_markup=keys,
    )
    assert len(keys.inline_keyboard) == 2


async def test_unrelated_bad_request_does_not_resend_manual_payment(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    bot = _bot()
    bot.send_rich_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=100, text="original"),
        message="Bad Request: chat not found",
    )
    with pytest.raises(TelegramBadRequest):
        await ui.send_manual_checkout(
            bot, chat_id=100, rich_html="<h3>Rich</h3>",
            legacy_html="Classic", instruction="2204240225046459",
            reply_markup=_keys(),
        )
    bot.send_message.assert_not_awaited()


async def test_master_switch_off_keeps_original_html_and_callbacks(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: False)
    bot = _bot()
    keys = _keys()
    await ui.send_manual_checkout(
        bot, chat_id=100, rich_html="<h3>Rich</h3>", legacy_html="Classic",
        instruction="2204240225046459", reply_markup=keys,
    )
    bot.send_rich_message.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        100, "Classic", parse_mode="HTML", reply_markup=keys,
    )


@asynccontextmanager
async def _fake_scope(*args, **kwargs):
    yield MagicMock()


async def test_subscription_handler_keeps_invoice_and_receipt_buttons(monkeypatch):
    project, price_id, method_id, request_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    request = SimpleNamespace(
        id=request_id, final_amount=Decimal("550"), currency_code="RUB",
    )
    option = SimpleNamespace(
        plan_name="1 месяц", duration_value=1, duration_unit="month",
        currency_precision=2, payment_method_name="Ozon Банк",
    )
    checkout = SimpleNamespace(
        request=request, option=option,
        instruction="Оплатите 550 RUB\n2204240225046459",
    )
    monkeypatch.setattr(
        plan_ui, "system_project_session_scope", _fake_scope,
    )
    monkeypatch.setattr(
        plan_ui, "create_checkout_for_telegram_user",
        AsyncMock(return_value=checkout),
    )
    send = AsyncMock()
    monkeypatch.setattr(plan_ui, "send_manual_checkout", send)
    router = plan_ui.create_telegram_commerce_router(MagicMock(), project_id=project)
    handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "buy_handler"
    )
    callback = SimpleNamespace(
        data="buy:" + encode_uuid(price_id) + ":" + encode_uuid(method_id),
        from_user=SimpleNamespace(id=100, username="buyer", first_name="Buyer"),
        answer=AsyncMock(),
    )
    await handler(callback, _bot())
    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs["chat_id"] == 100
    assert "550 RUB" in kwargs["rich_html"]
    assert "<tr><th>💳 Способ</th><td>Ozon Банк</td></tr>" in kwargs["rich_html"]
    assert "Оплатите 550 RUB" in kwargs["legacy_html"]
    assert kwargs["instruction"] == checkout.instruction
    buttons = [
        b for row in kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert [b.callback_data for b in buttons] == [
        "paid:" + encode_uuid(request_id),
        "cancel:" + encode_uuid(request_id),
        "checkoutnav:catalog",
    ]
    assert buttons[0].style == "success"
    assert buttons[1].style == "danger"


async def test_file_handler_uses_red_cancel_and_same_order_callbacks(monkeypatch):
    project, product, method, order_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    order = SimpleNamespace(
        id=order_id, status="pending", product_name="Моя жопа",
        currency_code="RUB", payment_method_name="Ozon Банк",
        amount=Decimal("100"), instruction="Оплатите 100 RUB\n2204240225046459",
    )
    monkeypatch.setattr(
        file_ui, "create_manual_order", AsyncMock(return_value=(order, False)),
    )
    send = AsyncMock()
    monkeypatch.setattr(file_ui, "send_manual_checkout", send)
    router = file_ui.create_manual_file_router(MagicMock(), project_id=project)
    handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "file_manual_callback"
    )
    query = SimpleNamespace(
        data=("fm:method:" + encode_uuid(product) + ":RUB:"
              + encode_uuid(method)),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
        from_user=SimpleNamespace(id=123, username="buyer", first_name="Buyer"),
        answer=AsyncMock(),
    )
    await handler(query, _bot())
    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs["instruction"] == order.instruction
    assert "<tr><th>🏷 Название</th><td>Моя жопа</td></tr>" in kwargs["rich_html"]
    assert "100 RUB" in kwargs["rich_html"]
    assert "Оплатите 100 RUB" in kwargs["legacy_html"]
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == [
        "fm:paid:" + encode_uuid(order_id),
        "fm:cancel:" + encode_uuid(order_id),
    ]
    assert buttons[0].style == "success"
    assert buttons[1].style == "danger"
