"""Broadcast post serialization, /start capture and Telegram payload tests."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, MessageEntity, User

from telegram_membership_saas.modules.broadcasts.handlers import (
    StartAudienceMiddleware,
)
from telegram_membership_saas.modules.broadcasts.service import extract_post
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


def test_protected_forward_or_album_is_rejected_not_partially_saved():
    with pytest.raises(ValueError, match="защищ"):
        extract_post(_post(text="Private", protected=True))
    with pytest.raises(ValueError, match="Альбом"):
        extract_post(_post(text="First", group="same-album"))
    with pytest.raises(ValueError, match="Поддерживаются"):
        extract_post(_post())


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
    assert await middleware(downstream, msg.model_copy(update={
        "chat": Chat(id=-100111, type="supergroup")
    }), {"bot": bot}) == "handled"
    record.assert_not_awaited()
