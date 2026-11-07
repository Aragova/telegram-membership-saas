"""Telegram support conversations are two-way, private and tenant scoped."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from test_owner_subscribers_ui import _bot, _detail

from telegram_membership_saas import localization
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.conversations import (
    ADMIN_MESSAGE_HEADER,
    ADMIN_MESSAGE_HEADER_EN,
    BUYER_MESSAGE_HEADER,
    BUYER_MESSAGE_HEADER_EN,
    BUYER_REPLY_PROMPT,
    BUYER_REPLY_PROMPT_EN,
    BuyerConversation,
    customer_ref,
    is_admin_message,
    parse_buyer_notification,
)


@pytest.fixture(autouse=True)
def fake_outbound_locale(monkeypatch):
    """Use stable locale for unit tests without a database connection."""
    @asynccontextmanager
    async def locale(factory, telegram_id):
        async with localization.recipient_language(None, telegram_id):
            yield

    monkeypatch.setattr(ui, "recipient_language", locale)


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
    assert is_admin_message(ADMIN_MESSAGE_HEADER_EN + "\n\nHello")
    assert is_admin_message(BUYER_REPLY_PROMPT + "\n\nReply")
    assert is_admin_message(BUYER_REPLY_PROMPT_EN + "\n\nReply")
    assert not is_admin_message("Hello\n" + ADMIN_MESSAGE_HEADER)
    assert parse_buyer_notification(
        BUYER_MESSAGE_HEADER + code + "\nHello"
    ) == buyer.customer_telegram_id
    with pytest.raises(ValueError):
        parse_buyer_notification("Random bot message " + code)
    assert parse_buyer_notification(
        BUYER_MESSAGE_HEADER_EN + code + "\nHello"
    ) == buyer.customer_telegram_id
    with pytest.raises(ValueError):
        parse_buyer_notification(BUYER_MESSAGE_HEADER + "/u0")


async def test_reply_button_uses_nonsticky_one_shot_composer(monkeypatch):
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
    controls = bot.send_message.await_args.kwargs["reply_markup"]
    assert controls.inline_keyboard[0][0].callback_data == "chat:cancel"
    assert "force_reply" not in str(controls.model_dump(exclude_none=True))
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


async def test_plain_next_reply_is_captured_once_and_composer_is_not_forced(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(ui, "find_buyer_conversation", AsyncMock(return_value=buyer))
    router = _dialog_router(project)
    callback = _handler(router, "buyer_reply_button", callbacks=True)
    handler_entry = next(
        h for h in router.message.handlers
        if h.callback.__name__ == "buyer_conversation_reply"
    )
    filt = next(
        f.callback for f in handler_entry.filters
        if isinstance(f.callback, ui.BuyerConversationReplyFilter)
    )
    reply_handler = handler_entry.callback
    bot = _bot()
    query = SimpleNamespace(
        data="chat:reply",
        message=SimpleNamespace(chat=SimpleNamespace(
            type="private", id=12345678,
        )),
        from_user=_user(12345678), answer=AsyncMock(),
    )
    plain = _message(uid=12345678, text="Привет! 👋")
    assert not await filt(plain, bot)
    await callback(query, bot)
    prompt = bot.send_message.await_args
    assert "force_reply" not in str(prompt.kwargs["reply_markup"].model_dump())
    assert await filt(plain, bot)
    await reply_handler(plain, bot)
    assert not await filt(plain, bot)
    assert any(call.args[0] == 123 for call in bot.send_message.await_args_list)


async def test_reply_mode_can_be_canceled_and_other_replies_are_not_stolen(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(ui, "find_buyer_conversation", AsyncMock(return_value=buyer))
    router = _dialog_router(project)
    activate = _handler(router, "buyer_reply_button", callbacks=True)
    cancel = _handler(router, "buyer_cancel_reply", callbacks=True)
    handler_entry = next(
        h for h in router.message.handlers
        if h.callback.__name__ == "buyer_conversation_reply"
    )
    filt = next(
        f.callback for f in handler_entry.filters
        if isinstance(f.callback, ui.BuyerConversationReplyFilter)
    )
    bot = _bot()
    query = SimpleNamespace(
        data="chat:reply",
        message=SimpleNamespace(chat=SimpleNamespace(
            type="private", id=12345678,
        )),
        from_user=_user(12345678), answer=AsyncMock(),
    )
    await activate(query, bot)
    quoted_receipt = _message(
        uid=12345678, text="receipt ref", parent="🧾 Payment request",
    )
    quoted_receipt.reply_to_message.from_user.id = bot.id
    assert not await filt(quoted_receipt, bot)
    plain = _message(uid=12345678, text="Test reply")
    assert await filt(plain, bot)
    query.data = "chat:cancel"
    await cancel(query, bot)
    assert not await filt(plain, bot)


async def test_english_buyer_gets_only_english_in_message_and_reply_prompt(monkeypatch):
    buyer = _buyer()
    project, actor = uuid4(), uuid4()
    monkeypatch.setattr(ui, "find_buyer_conversation", AsyncMock(
        return_value=buyer,
    ))
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "customer_id_by_telegram_id", AsyncMock(
        return_value=buyer.customer_id,
    ))
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(
        return_value=_detail(buyer.customer_id, uuid4()),
    ))

    @asynccontextmanager
    async def english_buyer_locale(factory, telegram_id):
        token = localization._language.set(
            "en" if telegram_id == buyer.customer_telegram_id else "ru",
        )
        try:
            yield
        finally:
            localization._language.reset(token)

    monkeypatch.setattr(ui, "recipient_language", english_buyer_locale)
    router = _dialog_router(project)
    bot = _bot()
    owner_reply = _handler(router, "owner_conversation_reply")
    previous = BUYER_MESSAGE_HEADER + customer_ref(buyer)
    await owner_reply(_message(
        uid=123, text="Hello there!", parent=previous,
    ), bot)
    sent = next(call for call in bot.send_message.await_args_list
                if call.args[0] == buyer.customer_telegram_id)
    assert sent.args[1].startswith(ADMIN_MESSAGE_HEADER_EN)
    assert "Reply to this message" in sent.args[1]
    assert "Ответьте" not in sent.args[1]
    assert sent.kwargs["reply_markup"].inline_keyboard[0][0].text == "↩️ Reply"

    # Incoming callback is already localized by LanguageMiddleware.
    button = _handler(router, "buyer_reply_button", callbacks=True)
    query = SimpleNamespace(
        data="chat:reply",
        message=SimpleNamespace(chat=SimpleNamespace(
            type="private", id=buyer.customer_telegram_id,
        )),
        from_user=_user(buyer.customer_telegram_id), answer=AsyncMock(),
    )
    token = localization._language.set("en")
    try:
        await button(query, bot)
    finally:
        localization._language.reset(token)
    assert bot.send_message.await_args.args[1].startswith(BUYER_REPLY_PROMPT_EN)
    assert "Введите сообщение" not in bot.send_message.await_args.args[1]


async def test_english_owner_notification_uses_owner_locale(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(ui, "find_buyer_conversation", AsyncMock(
        return_value=buyer,
    ))

    @asynccontextmanager
    async def locale(factory, telegram_id):
        token = localization._language.set(
            "en" if telegram_id == buyer.owner_telegram_id else "ru",
        )
        try:
            yield
        finally:
            localization._language.reset(token)

    monkeypatch.setattr(ui, "recipient_language", locale)
    handler = _handler(_dialog_router(project), "buyer_conversation_reply")
    bot = _bot()
    await handler(_message(
        uid=buyer.customer_telegram_id, text="Yes",
        parent=ADMIN_MESSAGE_HEADER,
    ), bot)
    outgoing = next(c for c in bot.send_message.await_args_list
                    if c.args[0] == buyer.owner_telegram_id)
    assert outgoing.args[1].startswith(BUYER_MESSAGE_HEADER_EN)
    assert [b.text for b in outgoing.kwargs["reply_markup"].inline_keyboard[0]] == [
        "✉️ Reply", "👤 Customer card",
    ]


async def test_legacy_force_reply_prompt_is_deleted_after_answer(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(ui, "find_buyer_conversation", AsyncMock(
        return_value=buyer,
    ))
    handler = _handler(_dialog_router(project), "buyer_conversation_reply")
    bot = _bot()
    bot.delete_message = AsyncMock()
    message = _message(
        uid=buyer.customer_telegram_id, text="Answer from old prompt",
        parent=BUYER_REPLY_PROMPT + "\n\nPlease reply",
    )
    message.reply_to_message.message_id = 42
    message.reply_to_message.from_user.id = bot.id
    await handler(message, bot)
    bot.delete_message.assert_awaited_once_with(buyer.customer_telegram_id, 42)
