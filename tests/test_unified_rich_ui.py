"""Project-wide Rich Messages: central, admin, buyer and native edit navigation."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas import rich_messages as rich
from telegram_membership_saas.customer_runtime import ObservedCustomerBot
from telegram_membership_saas.modules.telegram_commerce.service import CheckoutOption
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    build_buyer_offers,
    rich_catalog_html,
)
from telegram_membership_saas.rich_messages import RichUiBot


def _keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Pay", callback_data="buy:abc:def"),
    ]])


def _settings(on=True):
    return SimpleNamespace(rich_messages_enabled=on)


def _bot():
    return RichUiBot(token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")


def test_every_customer_bot_inherits_global_rich_transport():
    assert issubclass(ObservedCustomerBot, RichUiBot)


def test_rich_first_party_metrics_are_tables_in_both_languages():
    for title in ("ViMi Pay · Платформа", "ViMi Pay · Platform"):
        raw = (
            "✦ <b>" + title + "</b>\n"
            "🤖 Bots: <b>8</b>\n"
            "👥 Owners: <b>3</b>\n\n"
            "Confirmed turnover: 100 RUB"
        )
        result = rich.to_rich_html(raw)
        assert result.startswith("<h3>✦ <b>" + title + "</b></h3>")
        assert "<table bordered compact>" in result
        assert "<tr><th>🤖 Bots</th><td><b>8</b></td></tr>" in result
        assert "<tr><th>👥 Owners</th><td><b>3</b></td></tr>" in result
        assert "Confirmed turnover: 100 RUB" in result


def test_owner_payment_instructions_remain_byte_identical_after_heading():
    instruction = "Оплатите 500 RUB\nСБП: +7 999 000-00-00\nhttp://example.org/?x=1&amp;y=2"
    legacy = "💎 <b>Оформление подписки</b>\n\n" + instruction
    rendered = rich.to_rich_html(legacy)
    assert "Оплатите 500 RUB<br>СБП: +7 999 000-00-00" in rendered
    assert "http://example.org/?x=1&amp;y=2</p>" in rendered
    assert "<table" not in rendered


async def test_rich_send_keeps_exact_inline_callbacks_and_prices(monkeypatch):
    monkeypatch.setattr(rich, "get_settings", lambda: _settings())
    send_rich = AsyncMock(return_value="rich result")
    legacy = AsyncMock(return_value="classic result")
    monkeypatch.setattr(RichUiBot, "send_rich_message", send_rich)
    monkeypatch.setattr(Bot, "send_message", legacy)
    bot = _bot()
    try:
        result = await bot.send_message(
            123, "💎 <b>Подписки</b>\n500 RUB",
            parse_mode="HTML", reply_markup=_keyboard(),
        )
        assert result == "rich result"
        assert send_rich.await_count == 1
        assert send_rich.await_args.kwargs["reply_markup"].inline_keyboard[
            0
        ][0].callback_data == "buy:abc:def"
        assert "<h3>" in send_rich.await_args.kwargs["rich_message"].html
        legacy.assert_not_awaited()
    finally:
        await bot.session.close()


async def test_rich_edit_updates_single_message_and_preserves_keyboard(monkeypatch):
    monkeypatch.setattr(rich, "get_settings", lambda: _settings())
    edited = AsyncMock(return_value="edited")
    legacy = AsyncMock(return_value="classic edited")
    monkeypatch.setattr(RichUiBot, "__call__", edited)
    monkeypatch.setattr(Bot, "edit_message_text", legacy)
    bot = _bot()
    keyboard = _keyboard()
    try:
        result = await bot.edit_message_text(
            text="💎 <b>Подписка</b>\nЦена: 500 RUB",
            chat_id=123, message_id=77, parse_mode="HTML",
            reply_markup=keyboard,
        )
        assert result == "edited"
        payload = edited.await_args.args[0]
        assert isinstance(payload, EditMessageText)
        assert payload.message_id == 77
        assert payload.rich_message.html.startswith("<h3>")
        assert payload.reply_markup is keyboard
        assert payload.text is None
        legacy.assert_not_awaited()
    finally:
        await bot.session.close()


async def test_unsupported_rich_uses_original_html_exactly_once(monkeypatch):
    monkeypatch.setattr(rich, "get_settings", lambda: _settings())
    send_rich = AsyncMock(side_effect=TelegramBadRequest(
        method=SendMessage(chat_id=123, text="invalid"),
        message="Bad Request: can't parse Rich HTML",
    ))
    legacy = AsyncMock(return_value="classic")
    monkeypatch.setattr(RichUiBot, "send_rich_message", send_rich)
    monkeypatch.setattr(Bot, "send_message", legacy)
    bot = _bot()
    keys = _keyboard()
    try:
        text = "💎 <b>Subscribe</b>\nPay 100 RUB"
        assert await bot.send_message(
            123, text, parse_mode="HTML", reply_markup=keys,
        ) == "classic"
        legacy.assert_awaited_once_with(
            123, text, parse_mode="HTML", reply_markup=keys,
        )
        assert send_rich.await_count == 1
    finally:
        await bot.session.close()


async def test_transport_failures_never_resend_payment_ui(monkeypatch):
    monkeypatch.setattr(rich, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        RichUiBot, "send_rich_message",
        AsyncMock(side_effect=ConnectionError("network timeout")),
    )
    legacy = AsyncMock()
    monkeypatch.setattr(Bot, "send_message", legacy)
    bot = _bot()
    try:
        with pytest.raises(ConnectionError):
            await bot.send_message(
                123, "500 RUB", parse_mode="HTML", reply_markup=_keyboard(),
            )
        legacy.assert_not_awaited()
    finally:
        await bot.session.close()


async def test_master_rollback_and_classic_context(monkeypatch):
    monkeypatch.setattr(rich, "get_settings", lambda: _settings(False))
    send_rich = AsyncMock()
    legacy = AsyncMock()
    monkeypatch.setattr(RichUiBot, "send_rich_message", send_rich)
    monkeypatch.setattr(Bot, "send_message", legacy)
    bot = _bot()
    try:
        await bot.send_message(123, "Classic", reply_markup=_keyboard())
        send_rich.assert_not_awaited()
        legacy.assert_awaited_once()
        legacy.reset_mock()
        monkeypatch.setattr(rich, "get_settings", lambda: _settings(True))
        with rich.classic_messages():
            await bot.send_message(
                123, "<b>Old</b>", reply_markup=_keyboard(), parse_mode="HTML",
            )
        send_rich.assert_not_awaited()
        legacy.assert_awaited_once()
    finally:
        await bot.session.close()


async def test_plain_text_is_escaped_and_receipt_prompt_stays_classic(monkeypatch):
    monkeypatch.setattr(rich, "get_settings", lambda: _settings())
    send_rich = AsyncMock()
    legacy = AsyncMock()
    monkeypatch.setattr(RichUiBot, "send_rich_message", send_rich)
    monkeypatch.setattr(Bot, "send_message", legacy)
    bot = _bot()
    try:
        await bot.send_message(
            123, "Goods < & >", reply_markup=_keyboard(),
        )
        html = send_rich.await_args.kwargs["rich_message"].html
        assert "&lt;" in html and "&amp;" in html and "&gt;" in html
        from aiogram.types import ForceReply
        await bot.send_message(
            123, "Reply to this prompt", reply_markup=ForceReply(),
        )
        legacy.assert_awaited_once()
    finally:
        await bot.session.close()


@pytest.mark.parametrize("locale,tag,period", [
    ("ru", "Тариф", "1 месяц"),
    ("en", "Plan", "1 month"),
])
def test_complete_rich_catalog_uses_real_prices_and_localization(
    locale, tag, period,
):
    from telegram_membership_saas.localization import _language
    plan = uuid4()
    options = (
        CheckoutOption(
            plan_id=plan, plan_name="1 месяц", duration_value=1,
            duration_unit="month", plan_price_id=uuid4(),
            currency_code="RUB", amount=Decimal("500.00"),
            currency_precision=2, payment_method_id=uuid4(),
            payment_method_name="Bank",
        ),
        CheckoutOption(
            plan_id=plan, plan_name="1 месяц", duration_value=1,
            duration_unit="month", plan_price_id=uuid4(),
            currency_code="USD", amount=Decimal("10.00"),
            currency_precision=2, payment_method_id=uuid4(),
            payment_method_name="PayPal",
        ),
    )
    offers = build_buyer_offers(options, {plan: ("ONLY <YOU> & FULL",)})
    token = _language.set(locale)
    try:
        html = rich_catalog_html(offers, currency="RUB")
    finally:
        _language.reset(token)
    assert html is not None and "<table bordered compact>" in html
    assert tag in html and period in html
    assert "500 RUB" in html and "10 USD" not in html
    assert "ONLY &lt;YOU&gt; &amp; FULL" in html
    assert html.count("1 месяц") <= 1


async def test_old_aiogram_without_rich_type_still_starts_in_classic_mode(monkeypatch):
    monkeypatch.setattr(rich, "get_settings", lambda: _settings())
    monkeypatch.setattr(rich, "InputRichMessage", None)
    classic = AsyncMock()
    monkeypatch.setattr(Bot, "send_message", classic)
    bot = _bot()
    try:
        await bot.send_message(
            123, "💎 <b>Old</b>", parse_mode="HTML", reply_markup=_keyboard(),
        )
        classic.assert_awaited_once()
        assert not rich._eligible("text", {"reply_markup": _keyboard()})
    finally:
        await bot.session.close()


@pytest.mark.parametrize("prefix", [
    "💬 ViMi Pay · Покупатель /u12345",
    "💬 ViMi Pay · Customer /u12345",
    "✉️ ViMi Pay · Сообщение администратора",
    "✉️ ViMi Pay · Message from admin",
    "✍️ ViMi Pay · Ответ администратору",
    "✍️ ViMi Pay · Reply to admin",
])
async def test_native_support_replies_remain_readable_text(monkeypatch, prefix):
    monkeypatch.setattr(rich, "get_settings", lambda: _settings())
    rich_send = AsyncMock()
    classic = AsyncMock()
    monkeypatch.setattr(RichUiBot, "send_rich_message", rich_send)
    monkeypatch.setattr(Bot, "send_message", classic)
    bot = _bot()
    try:
        await bot.send_message(
            123, prefix + "\nReply safely",
            reply_markup=_keyboard(),
        )
        rich_send.assert_not_awaited()
        classic.assert_awaited_once()
    finally:
        await bot.session.close()


async def test_both_explicit_previews_obey_platform_rollback(monkeypatch):
    from unittest.mock import MagicMock

    from telegram_membership_saas.modules.file_sales import rich_ui as file_rich
    from telegram_membership_saas.modules.telegram_commerce import rich_ui as plan_rich

    settings = _settings(False)
    monkeypatch.setattr(file_rich, "get_settings", lambda: settings)
    monkeypatch.setattr(plan_rich, "get_settings", lambda: settings)
    bot = MagicMock(id=123)
    bot.send_message = AsyncMock()
    bot.send_rich_message = AsyncMock()
    markup = _keyboard()
    assert not await file_rich.send_file_card(
        bot, chat_id=123, rich_html="<h3>Rich</h3>",
        legacy_html="<b>Classic</b>", reply_markup=markup, preview=True,
    )
    assert not await plan_rich.send_rich_subscription_offer(
        bot, chat_id=123, rich_html="<h3>Rich</h3>",
        reply_markup=markup, preview=True,
    )
    bot.send_rich_message.assert_not_awaited()
    bot.send_message.assert_awaited_once()


async def test_shared_language_selector_uses_rich_capable_bot(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from telegram_membership_saas.localization import create_language_router

    router = create_language_router(MagicMock())
    command = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "language_command"
    )
    callback = next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "language_menu"
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    user = SimpleNamespace(id=123)
    msg = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=123),
        from_user=user, answer=AsyncMock(),
    )
    await command(msg, bot)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    msg.answer.assert_not_awaited()
    bot.send_message.reset_mock()
    query = SimpleNamespace(
        from_user=user, message=msg, answer=AsyncMock(),
    )
    await callback(query, bot)
    bot.send_message.assert_awaited_once()
    query.answer.assert_awaited_once()
    msg.answer.assert_not_awaited()


def test_rich_html_multiline_preserves_visual_breaks_and_preformatted_code():
    body = (
        "💎 <b>Checkout</b>\n\n"
        "Pay 100 RUB\nBank number: 123 456\n\n"
        "<pre>code\nwith deliberate indentation</pre>\n"
        "Then tap Paid"
    )
    html = rich.to_rich_html(body)
    assert "<h3>💎 <b>Checkout</b></h3>" in html
    assert "Pay 100 RUB<br>Bank number: 123 456" in html
    assert "<pre>code\nwith deliberate indentation</pre>" in html
    assert "Then tap Paid" in html
    assert "<br>with deliberate indentation</pre>" not in html


async def test_failed_rich_catalog_does_not_trigger_second_rich_send(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    from telegram_membership_saas.modules.telegram_commerce import handlers as ui

    monkeypatch.setattr(rich, "get_settings", lambda: _settings())
    project, plan, price, method = uuid4(), uuid4(), uuid4(), uuid4()
    options = (
        CheckoutOption(
            plan_id=plan, plan_name="1 месяц", duration_value=1,
            duration_unit="month", plan_price_id=price,
            currency_code="RUB", amount=Decimal("500"),
            currency_precision=2, payment_method_id=method,
            payment_method_name="Bank",
        ),
    )
    offers = build_buyer_offers(options, {plan: ("Test channel",)})

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield MagicMock()

    monkeypatch.setattr(ui, "system_project_session_scope", scope)
    monkeypatch.setattr(ui, "list_buyer_offers", AsyncMock(return_value=offers))
    monkeypatch.setattr(ui, "rich_subscription_enabled", lambda _id: True)
    rich_attempt = AsyncMock(return_value=False)
    monkeypatch.setattr(ui, "send_rich_subscription_offer", rich_attempt)

    async def confirm_classic_fallback(*args, **kwargs):
        assert rich._skip_rich.get() is True

    classic_fallback = AsyncMock(side_effect=confirm_classic_fallback)
    monkeypatch.setattr(ui, "_send_screen", classic_fallback)
    bot = _bot()
    try:
        result = await ui._send_catalog(
            bot, chat_id=123, session_factory=MagicMock(),
            project_id=project, price_id=price, message_id=77,
        )
        assert result
        rich_attempt.assert_awaited_once()
        classic_fallback.assert_awaited_once()
    finally:
        await bot.session.close()
