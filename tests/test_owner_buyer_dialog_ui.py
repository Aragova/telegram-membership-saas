"""Telegram support conversations are two-way, private and tenant scoped."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.conversations import (
    ADMIN_MESSAGE_HEADER,
    BUYER_MESSAGE_HEADER,
    BUYER_REPLY_PROMPT,
    BuyerConversation,
    customer_ref,
    is_admin_message,
    parse_buyer_notification,
)
from test_owner_subscribers_ui import _bot, _detail


def _dialog_router(project):
    return ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)


def _handler(router, name, *, callbacks=False):
    handlers = router.callback_query.handlers if callbacks else router.message.handlers
    return next(h.callback for h in handlers if h.callback.__name__ == name)


def _user(uid, username=None):
    return SimpleNamespace(id=uid, username=username, first_name="Visitor")


def _message(*, uid, text, parent=None):
    return SimpleNamespace(
        text=text, from_user=_user(uid, "buyer" if uid == 12345678 else "owner"),
        chat=SimpleNamespace(type="private", id=uid),
        reply_to_message=SimpleNamespace(
            text=parent, from_user=SimpleNamespace(id=44444),
        ) if parent else None,
    )


def _buyer(project_id=None):
    return BuyerConversation(
        customer_id=uuid4(), customer_telegram_id=12345678,
        username="buyer", first_name="Buyer", owner_telegram_id=123,
        is_banned=False,
    )


def test_trusted_dialog_first_line_and_customer_reference():
    buyer = _buyer()
    code = customer_ref(buyer)
    assert code.startswith("/u")
    assert is_admin_message(ADMIN_MESSAGE_HEADER + "\n\nHello")
    assert is_admin_message(BUYER_REPLY_PROMPT + "\n\nReply")
    assert not is_admin_message("Hello\n" + ADMIN_MESSAGE_HEADER)
    assert parse_buyer_notification(
        BUYER_MESSAGE_HEADER + code + "\nHello"
    ) == buyer.customer_telegram_id
    with pytest.raises(ValueError):
        parse_buyer_notification("Random bot message " + code)
    with pytest.raises(ValueError):
        parse_buyer_notification(BUYER_MESSAGE_HEADER + "/u0")


async def test_buyer_reply_button_opens_force_reply_for_customer(monkeypatch):
    project = uuid4()
    buyer = _buyer()
    monkeypatch.setattr(ui, "find_buyer_conversation", AsyncMock(
        return_value=buyer,
    ))
    handler = _handler(_dialog_router(project), "buyer_reply_button", callbacks=True)
    bot = _bot()
    query = SimpleNamespace(
        data="chat:reply",
        message=SimpleNamespace(chat=SimpleNamespace(
            type="private", id=12345678,
        )),
        from_user=_user(12345678), answer=AsyncMock(),
    )
    await handler(query, bot)
    assert bot.send_message.await_args.args[0] == 12345678
    assert bot.send_message.await_args.args[1].startswith(BUYER_REPLY_PROMPT)
    assert bot.send_message.await_args.kwargs["reply_markup"].force_reply
    query.answer.assert_awaited_once()


async def test_buyer_reply_reaches_owner_with_reply_and_card_actions(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(ui, "find_buyer_conversation", AsyncMock(
        return_value=buyer,
    ))
    handler = _handler(_dialog_router(project), "buyer_conversation_reply")
    bot = _bot()
    await handler(_message(
        uid=12345678, text="КУ КУ, получил! 👋",
        parent=ADMIN_MESSAGE_HEADER + "\n\nHi",
    ), bot)
    calls = bot.send_message.await_args_list
    owner_send = next(c for c in calls if c.args[0] == 123)
    assert owner_send.args[1].startswith(BUYER_MESSAGE_HEADER + customer_ref(buyer))
    assert "КУ КУ, получил! 👋" in owner_send.args[1]
    actions = [
        button.callback_data for row in owner_send.kwargs[
            "reply_markup"].inline_keyboard for button in row
    ]
    assert "own:submsg:" in actions[0]
    assert "own:subscriber:" in actions[1]
    assert any(c.args[0] == 12345678 and "✅" in c.args[1] for c in calls)


async def test_banned_buyer_cannot_send_support_replies(monkeypatch):
    project, buyer = uuid4(), _buyer()
    buyer = BuyerConversation(
        customer_id=buyer.customer_id,
        customer_telegram_id=buyer.customer_telegram_id,
        username=buyer.username, first_name=buyer.first_name,
        owner_telegram_id=buyer.owner_telegram_id, is_banned=True,
    )
    monkeypatch.setattr(ui, "find_buyer_conversation", AsyncMock(
        return_value=buyer,
    ))
    handler = _handler(_dialog_router(project), "buyer_conversation_reply")
    bot = _bot()
    await handler(_message(
        uid=12345678, text="Spam", parent=ADMIN_MESSAGE_HEADER,
    ), bot)
    assert all(call.args[0] != 123 for call in bot.send_message.await_args_list)


async def test_owner_reply_to_notification_delivered_to_same_buyer(monkeypatch):
    project, buyer = uuid4(), _buyer()
    actor = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=actor,
    ))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(
        return_value=True,
    ))
    resolve = AsyncMock(return_value=buyer.customer_id)
    monkeypatch.setattr(ui, "customer_id_by_telegram_id", resolve)
    detail = AsyncMock(return_value=_detail(buyer.customer_id, uuid4()))
    monkeypatch.setattr(ui, "subscriber_detail", detail)
    router = _dialog_router(project)
    bot = _bot()
    handler = _handler(router, "owner_conversation_reply")
    await handler(_message(
        uid=123, text="Привет, получил ответ.",
        parent=BUYER_MESSAGE_HEADER + customer_ref(buyer) + "\n👤 @buyer",
    ), bot)
    resolve.assert_awaited_once()
    assert resolve.await_args.kwargs["telegram_id"] == buyer.customer_telegram_id
    assert resolve.await_args.kwargs["project_id"] == project
    assert any(
        c.args[0] == 12345678 and c.args[1].startswith(ADMIN_MESSAGE_HEADER)
        and "Привет, получил ответ." in c.args[1]
        for c in bot.send_message.await_args_list
    )


async def test_non_owner_cannot_reply_even_with_valid_notification(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(
        return_value=False,
    ))
    detail = AsyncMock()
    monkeypatch.setattr(ui, "subscriber_detail", detail)
    handler = _handler(_dialog_router(project), "owner_conversation_reply")
    bot = _bot()
    await handler(_message(
        uid=123, text="Not authorized",
        parent=BUYER_MESSAGE_HEADER + customer_ref(buyer),
    ), bot)
    detail.assert_not_awaited()
    assert all(c.args[0] != 12345678 for c in bot.send_message.await_args_list)


async def test_reply_filter_requires_real_bot_message_in_private_chat():
    f = ui.BuyerConversationReplyFilter()
    bot = _bot()
    msg = _message(uid=12345678, text="Reply", parent=ADMIN_MESSAGE_HEADER)
    assert await f(msg, bot)
    msg.reply_to_message.from_user.id = 999
    assert not await f(msg, bot)
    msg.reply_to_message.from_user.id = bot.id
    msg.chat.type = "group"
    assert not await f(msg, bot)
    msg.chat.type = "private"
    msg.reply_to_message.text = "🧾 Payment confirmation for request"
    assert not await f(msg, bot)
    msg.reply_to_message.text = ADMIN_MESSAGE_HEADER
    msg.text = "/start"
    assert not await f(msg, bot)
