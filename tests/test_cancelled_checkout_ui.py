"""Closed manual payments must lose live actions without rewriting instructions."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas import localization
from telegram_membership_saas import manual_checkout_ui as ui
from telegram_membership_saas.modules.file_sales import manual_handlers as files
from telegram_membership_saas.modules.file_sales import manual_service
from telegram_membership_saas.modules.telegram_commerce import handlers as plans
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _bot():
    bot = MagicMock()
    bot.id = 8528484991
    bot.send_message = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def _callback(data: str, *, user_id: int = 100):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(
            id=user_id, username="buyer", first_name="Buyer",
        ),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=user_id),
            message_id=1234,
        ),
        answer=AsyncMock(),
    )


@pytest.mark.parametrize(("language", "status"), [
    ("ru", "❌ Заявка отменена"),
    ("en", "❌ Request canceled"),
])
def test_canceled_keyboard_contains_only_disabled_status_and_safe_navigation(
    language, status,
):
    if ui.DisabledButton is None:
        pytest.skip("Old aiogram lacks disabled buttons")
    nav = InlineKeyboardButton(
        text="💎 Browse plans", callback_data="checkoutnav:catalog",
    )
    token = localization._language.set(language)
    try:
        markup = ui.cancelled_checkout_keyboard(navigation=nav)
    finally:
        localization._language.reset(token)
    assert markup is not None
    assert markup.inline_keyboard[0][0].text == status
    assert markup.inline_keyboard[0][0].disabled is not None
    assert markup.inline_keyboard[0][0].style == "danger"
    assert markup.inline_keyboard[0][0].callback_data is None
    assert markup.inline_keyboard[1] == [nav]
    assert [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row if button.callback_data is not None
    ] == ["checkoutnav:catalog"]


async def test_retirement_replaces_all_file_payment_copy_and_cancel_buttons():
    bot = _bot()
    callback = _callback("fm:cancel:abc")
    assert await ui.retire_cancelled_checkout(bot, callback=callback)
    kwargs = bot.edit_message_reply_markup.await_args.kwargs
    assert kwargs["chat_id"] == 100
    assert kwargs["message_id"] == 1234
    markup = kwargs["reply_markup"]
    if ui.DisabledButton is not None:
        assert len(markup.inline_keyboard) == 1
        assert markup.inline_keyboard[0][0].disabled is not None
    else:
        assert markup is None
    # No active paid/cancel/copy control survives the edit.
    assert not any(
        button.callback_data or button.copy_text
        for row in markup.inline_keyboard for button in row
    ) if markup is not None else True
    bot.send_message.assert_not_awaited()


async def test_unknown_or_foreign_message_not_edited():
    bot = _bot()
    cb = _callback("cancel:id")
    cb.message.chat.id = 999
    assert not await ui.retire_cancelled_checkout(bot, callback=cb)
    cb.message.chat.id = 100
    cb.message.chat.type = "group"
    assert not await ui.retire_cancelled_checkout(bot, callback=cb)
    cb.message.chat.type = "private"
    cb.message.message_id = None
    assert not await ui.retire_cancelled_checkout(bot, callback=cb)
    bot.edit_message_reply_markup.assert_not_awaited()


async def test_old_aiogram_does_not_leave_payment_buttons(monkeypatch):
    monkeypatch.setattr(ui, "DisabledButton", None)
    bot = _bot()
    nav = InlineKeyboardButton(
        text="💎 Browse plans", callback_data="checkoutnav:catalog",
    )
    cb = _callback("cancel:id")
    assert await ui.retire_cancelled_checkout(
        bot, callback=cb, navigation=nav,
    )
    markup = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert [button.callback_data for row in markup.inline_keyboard
            for button in row] == ["checkoutnav:catalog"]
    bot.edit_message_reply_markup.reset_mock()
    assert await ui.retire_cancelled_checkout(bot, callback=cb)
    assert bot.edit_message_reply_markup.await_args.kwargs["reply_markup"] is None


async def test_unsupported_disabled_button_falls_back_to_nav_only():
    if ui.DisabledButton is None:
        pytest.skip("Old aiogram lacks disabled buttons")
    bot = _bot()
    bot.edit_message_reply_markup.side_effect = [
        TelegramBadRequest(
            method=EditMessageReplyMarkup(chat_id=100, message_id=1234),
            message="Bad Request: can't parse inline keyboard button: disabled",
        ),
        True,
    ]
    nav = InlineKeyboardButton(
        text="💎 Browse plans", callback_data="checkoutnav:catalog",
    )
    assert await ui.retire_cancelled_checkout(
        bot, callback=_callback("cancel:id"), navigation=nav,
    )
    assert bot.edit_message_reply_markup.await_count == 2
    last = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert [button.callback_data for row in last.inline_keyboard
            for button in row] == ["checkoutnav:catalog"]


async def test_edit_error_is_nonfatal_after_cancellation():
    bot = _bot()
    bot.edit_message_reply_markup.side_effect = TelegramBadRequest(
        method=EditMessageReplyMarkup(chat_id=100, message_id=1234),
        message="Bad Request: message to edit not found",
    )
    assert not await ui.retire_cancelled_checkout(
        bot, callback=_callback("cancel:id"),
    )


@asynccontextmanager
async def _scope(*args, **kwargs):
    yield MagicMock()


def _handler(router, name):
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == name
    )


async def test_subscription_cancel_changes_db_then_retires_and_returns_to_catalog(
    monkeypatch,
):
    order_id, project = uuid4(), uuid4()
    cancel = AsyncMock()
    retire = AsyncMock(return_value=True)
    catalog = AsyncMock()
    monkeypatch.setattr(plans, "system_project_session_scope", _scope)
    monkeypatch.setattr(plans, "cancel_checkout_for_telegram_user", cancel)
    monkeypatch.setattr(plans, "retire_cancelled_checkout", retire)
    monkeypatch.setattr(plans, "_send_catalog", catalog)
    router = plans.create_telegram_commerce_router(MagicMock(), project_id=project)
    callback = _callback("cancel:" + encode_uuid(order_id))
    await _handler(router, "cancel_handler")(callback, _bot())
    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["request_id"] == order_id
    assert cancel.await_args.kwargs["project_id"] == project
    retire.assert_awaited_once()
    nav = retire.await_args.kwargs["navigation"]
    assert nav.callback_data == "checkoutnav:catalog"
    assert nav.style == "primary"
    assert retire.await_args.kwargs["callback"] is callback
    catalog.assert_awaited_once()
    callback.answer.assert_awaited_once()



async def test_subscription_stale_callback_ack_does_not_block_card_retirement(
    monkeypatch,
):
    cancelled = AsyncMock()
    retired = AsyncMock(return_value=True)
    catalog = AsyncMock()
    monkeypatch.setattr(plans, "system_project_session_scope", _scope)
    monkeypatch.setattr(plans, "cancel_checkout_for_telegram_user", cancelled)
    monkeypatch.setattr(plans, "retire_cancelled_checkout", retired)
    monkeypatch.setattr(plans, "_send_catalog", catalog)
    router = plans.create_telegram_commerce_router(
        MagicMock(), project_id=uuid4(),
    )
    callback = _callback("cancel:" + encode_uuid(uuid4()))
    callback.answer.side_effect = TelegramBadRequest(
        method=AnswerCallbackQuery(callback_query_id="expired"),
        message="Bad Request: query is too old",
    )
    await _handler(router, "cancel_handler")(callback, _bot())
    cancelled.assert_awaited_once()
    retired.assert_awaited_once()
    catalog.assert_awaited_once()


async def test_subscription_denied_cancel_keeps_original_checkout_buttons(
    monkeypatch,
):
    cancel = AsyncMock(side_effect=ValueError("Request already processed"))
    retire = AsyncMock()
    catalog = AsyncMock()
    monkeypatch.setattr(plans, "system_project_session_scope", _scope)
    monkeypatch.setattr(plans, "cancel_checkout_for_telegram_user", cancel)
    monkeypatch.setattr(plans, "retire_cancelled_checkout", retire)
    monkeypatch.setattr(plans, "_send_catalog", catalog)
    router = plans.create_telegram_commerce_router(
        MagicMock(), project_id=uuid4(),
    )
    cb = _callback("cancel:" + encode_uuid(uuid4()))
    await _handler(router, "cancel_handler")(cb, _bot())
    retire.assert_not_awaited()
    catalog.assert_not_awaited()
    assert cb.answer.await_args.kwargs["show_alert"] is True


async def test_file_cancel_retires_only_confirmed_order(monkeypatch):
    cancel = AsyncMock()
    retire = AsyncMock(return_value=True)
    monkeypatch.setattr(manual_service, "cancel_manual_order", cancel)
    monkeypatch.setattr(files, "retire_cancelled_checkout", retire)
    router = files.create_manual_file_router(
        MagicMock(), project_id=uuid4(),
    )
    order_id = uuid4()
    cb = _callback("fm:cancel:" + encode_uuid(order_id))
    bot = _bot()
    await _handler(router, "file_manual_callback")(cb, bot)
    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["order_id"] == order_id
    retire.assert_awaited_once()
    assert retire.await_args.kwargs["callback"] is cb
    assert "navigation" not in retire.await_args.kwargs
    assert "Заявка отменена." in bot.send_message.await_args.args[1]
    cb.answer.assert_awaited_once()


async def test_file_cancel_rejection_keeps_live_checkout_unmodified(monkeypatch):
    cancel = AsyncMock(side_effect=ValueError("Only pending can cancel"))
    retire = AsyncMock()
    monkeypatch.setattr(manual_service, "cancel_manual_order", cancel)
    monkeypatch.setattr(files, "retire_cancelled_checkout", retire)
    router = files.create_manual_file_router(
        MagicMock(), project_id=uuid4(),
    )
    cb = _callback("fm:cancel:" + encode_uuid(uuid4()))
    bot = _bot()
    await _handler(router, "file_manual_callback")(cb, bot)
    retire.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert cb.answer.await_args.kwargs["show_alert"] is True
