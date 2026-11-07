"""Broadcast post serialization, /start capture and Telegram payload tests."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, MessageEntity, User

from telegram_membership_saas.modules.broadcasts.handlers import (
    StartAudienceMiddleware,
)
from telegram_membership_saas.modules.broadcasts.service import extract_album, extract_post
from telegram_membership_saas.modules.broadcasts.worker import _send_post


def _post(*, text=None, caption=None, photo=None, video=None, entities=None,
          group=None, protected=False):
    return SimpleNamespace(
        text=text, caption=caption,
        photo=photo, video=video, document=None, animation=None,
        entities=entities, caption_entities=entities,
        media_group_id=group, has_protected_content=protected,
    )


def test_forwarded_text_preserves_urls_and_rich_entities():
    link = MessageEntity(
        type="text_link", offset=0, length=5, url="https://t.me/channel"
    )
    post = _post(text="Привет 🎉 t.me/offer", entities=[link])
    kind, body, entities, file_id = extract_post(post)
    assert kind == "text" and file_id is None
    assert body == "Привет 🎉 t.me/offer"
    assert entities[0]["url"] == "https://t.me/channel"

    emoji = _post(text="Скидка 🍓 без подписки!")
    assert extract_post(emoji)[1] == emoji.text


def test_forwarded_photo_video_and_caption_are_copyable_without_downloading():
    caption = "Новое видео 🍉 https://t.me/test"
    photo = _post(
        caption=caption, photo=[SimpleNamespace(file_id="bot-owned-photo")],
    )
    video = _post(
        caption=caption, video=SimpleNamespace(file_id="bot-owned-video"),
    )
    assert extract_post(photo) == (
        "photo", caption, None, "bot-owned-photo",
    )
    assert extract_post(video) == (
        "video", caption, None, "bot-owned-video",
    )
    # Telegram's backward-compatible animation payload ALSO has a
    # document field; GIF must retain animation delivery semantics.
    gif = _post(caption="GIF 🎉")
    gif.document = SimpleNamespace(file_id="fallback-document")
    gif.animation = SimpleNamespace(file_id="real-animation")
    assert extract_post(gif) == (
        "animation", "GIF 🎉", None, "real-animation",
    )


def test_protected_forward_or_album_is_rejected_not_partially_saved():
    with pytest.raises(ValueError, match="защищ"):
        extract_post(_post(text="Private", protected=True))
    with pytest.raises(ValueError, match="Альбом"):
        extract_post(_post(text="First", group="same-album"))
    with pytest.raises(ValueError, match="Поддерживаются"):
        extract_post(_post())


def _album_item(*, number: int, kind: str = "photo", caption: str | None = None,
                group: str = "media-1", protected: bool = False):
    item = _post(caption=caption, group=group, protected=protected)
    item.message_id = number
    if kind == "photo":
        item.photo = [SimpleNamespace(file_id=f"photo-{number}")]
    elif kind == "video":
        item.video = SimpleNamespace(file_id=f"video-{number}")
    return item


def test_album_captures_every_photo_and_video_in_order_with_caption():
    msgs = [
        _album_item(number=4, kind="video"),
        _album_item(number=2, caption="Новый альбом 🌷"),
        _album_item(number=3),
    ]
    kind, caption, entities, file_id, items = extract_album(msgs)
    assert kind == "album" and caption == "Новый альбом 🌷"
    assert entities is None and file_id is None
    assert items == [
        {"type": "photo", "file_id": "photo-2"},
        {"type": "photo", "file_id": "photo-3"},
        {"type": "video", "file_id": "video-4"},
    ]


def test_album_rejects_partial_protected_or_mixed_groups():
    with pytest.raises(ValueError, match="2 до 10"):
        extract_album([_album_item(number=1)])
    with pytest.raises(ValueError, match="защищ"):
        extract_album([_album_item(number=1, protected=True),
                       _album_item(number=2)])
    with pytest.raises(ValueError, match="одному альбому"):
        extract_album([_album_item(number=1, group="a"),
                       _album_item(number=2, group="b")])
    with pytest.raises(ValueError, match="подпись"):
        extract_album([_album_item(number=1, caption="a"),
                       _album_item(number=2, caption="b")])
    with pytest.raises(ValueError, match="только фото и видео"):
        extract_album([_album_item(number=1), _album_item(number=2, kind="unknown")])


async def test_broadcast_worker_sends_album_as_one_group():
    bot = SimpleNamespace(send_media_group=AsyncMock())
    await _send_post(
        bot, user_id=789,
        content=SimpleNamespace(
            kind="album", text="Описание 🩷", entities=None, file_id=None,
            media_items=[
                {"type": "photo", "file_id": "a"},
                {"type": "video", "file_id": "b"},
            ],
        ),
    )
    bot.send_media_group.assert_awaited_once()
    kwargs = bot.send_media_group.await_args.kwargs
    assert kwargs["chat_id"] == 789
    assert len(kwargs["media"]) == 2
    assert kwargs["media"][0].media == "a"
    assert kwargs["media"][0].caption == "Описание 🩷"
    assert kwargs["media"][1].media == "b"
    assert kwargs["media"][1].caption is None


async def test_broadcast_worker_uses_entities_not_unescaped_html():
    bot = SimpleNamespace(
        send_message=AsyncMock(), send_photo=AsyncMock(),
        send_video=AsyncMock(), send_document=AsyncMock(),
        send_animation=AsyncMock(),
    )
    entity = MessageEntity(
        type="text_link", offset=0, length=5, url="https://t.me/example"
    )
    await _send_post(
        bot, user_id=123,
        content=SimpleNamespace(
            kind="text", text="Hello <safe>", entities=[
                entity.model_dump(mode="json", exclude_none=True)
            ], file_id=None,
        ),
    )
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == "Hello <safe>"
    assert bot.send_message.await_args.kwargs["entities"][0].url == (
        "https://t.me/example"
    )
    await _send_post(
        bot, user_id=123,
        content=SimpleNamespace(
            kind="photo", text="caption 🍓", entities=None,
            file_id="file-from-this-bot",
        ),
    )
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["photo"] == "file-from-this-bot"
    await _send_post(
        bot, user_id=123,
        content=SimpleNamespace(
            kind="animation", text="GIF 🎉", entities=None,
            file_id="real-animation",
        ),
    )
    bot.send_animation.assert_awaited_once()
    assert bot.send_animation.await_args.kwargs["animation"] == "real-animation"


async def test_router_keeps_album_uncommitted_until_explicit_confirmation(monkeypatch):
    from telegram_membership_saas.modules.broadcasts import handlers

    actor = uuid4()
    project = uuid4()
    monkeypatch.setattr(handlers, "ensure_telegram_actor",
                        AsyncMock(return_value=actor))
    monkeypatch.setattr(handlers, "can_manage_project",
                        AsyncMock(return_value=True))
    monkeypatch.setattr(handlers, "get_draft", AsyncMock(return_value=SimpleNamespace(
        mode="create", template_id=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
    )))
    store = AsyncMock()
    monkeypatch.setattr(handlers, "save_from_message", store)

    router = handlers.create_broadcast_router(None, project_id=project)
    receive = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "receive_broadcast_post"
    )
    callback = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "broadcast_callback"
    )
    user = SimpleNamespace(id=1234, username=None, first_name="Admin")
    chat = SimpleNamespace(id=1234, type="private")
    bot = SimpleNamespace(id=5678, send_message=AsyncMock())

    def media(number):
        item = _album_item(number=number)
        item.from_user = user
        item.chat = chat
        return item

    await receive(media(1), bot)
    await receive(media(2), bot)
    await asyncio.sleep(1.75)
    store.assert_not_awaited()
    first_prompt = bot.send_message.await_args_list[-1]
    assert "2" in first_prompt.args[1]
    old_confirm = first_prompt.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    # A delayed third update arrives after the initial quiet period.
    await receive(media(3), bot)
    await asyncio.sleep(1.75)
    store.assert_not_awaited()
    last_prompt = bot.send_message.await_args_list[-1]
    assert "3" in last_prompt.args[1]
    assert old_confirm != last_prompt.kwargs[
        "reply_markup"].inline_keyboard[0][0].callback_data

    alert = AsyncMock()
    query = SimpleNamespace(
        data=old_confirm, from_user=user,
        message=SimpleNamespace(chat=chat), answer=alert,
    )
    await callback(query, bot)
    alert.assert_awaited_once()
    assert alert.await_args.kwargs["show_alert"] is True
    store.assert_not_awaited()

    # Discard current batch; it must never be persisted implicitly.
    query.data = last_prompt.kwargs[
        "reply_markup"].inline_keyboard[1][0].callback_data
    await callback(query, bot)
    store.assert_not_awaited()


async def test_start_middleware_only_registers_private_start(monkeypatch):
    from telegram_membership_saas.modules.broadcasts import handlers

    record = AsyncMock()
    monkeypatch.setattr(handlers, "record_bot_start", record)
    project = __import__("uuid").uuid4()
    middleware = StartAudienceMiddleware(None, project_id=project)
    downstream = AsyncMock(return_value="handled")
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1001, type="private"),
        from_user=User(id=1001, is_bot=False, first_name="Buyer"),
        text="/start special",
    )
    bot = SimpleNamespace(id=12345)
    assert await middleware(downstream, msg, {"bot": bot}) == "handled"
    record.assert_awaited_once_with(
        None, project_id=project, telegram_bot_id=12345,
        telegram_user_id=1001,
    )
    record.reset_mock()
    assert await middleware(downstream, msg.model_copy(update={"text": "/menu"}),
                            {"bot": bot}) == "handled"
    record.assert_not_awaited()
    # Media messages and photo/PDF payment receipts MUST pass unchanged.
    for media in ("photo", "video", "document"):
        media_msg = msg.model_copy(update={"text": None, media: ["fake"]})
        assert await middleware(
            downstream, media_msg, {"bot": bot},
        ) == "handled"
    record.assert_not_awaited()
    assert await middleware(downstream, msg.model_copy(update={
        "chat": Chat(id=-100111, type="supergroup")
    }), {"bot": bot}) == "handled"
    record.assert_not_awaited()
