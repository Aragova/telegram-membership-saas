"""Role-safe buyer home: show exact published folders only when available."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.telegram_commerce import handlers as buyer_ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    buyer_root_screen,
)
from telegram_membership_saas.support_links import SUPPORT_URL


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_buyer_root_screen_keeps_channel_first_and_folder_names_verbatim():
    folder_a, folder_b = uuid4(), uuid4()
    folders = ((folder_a, "📁 ёбка 🍓"), (folder_b, "НОВЫЕ ВИДЕО 🔥"))
    for language in ("ru", "en"):
        token = _language.set(language)
        try:
            text, keyboard = buyer_root_screen("ONLY YOU FULL ❤️", folders)
            buttons = _buttons(keyboard)
            assert len(buttons) == 5
            assert [b.text for b in buttons] == [
                "ONLY YOU FULL ❤️", "📁 ёбка 🍓", "НОВЫЕ ВИДЕО 🔥",
                "🌐 Language / Язык",
                "🆘 Поддержка" if language == "ru" else "🆘 Support",
            ]
            assert [b.callback_data for b in buttons] == [
                "buyer:channel",
                "fs:folder:" + encode_uuid(folder_a),
                "fs:folder:" + encode_uuid(folder_b),
                "lng:menu", None,
            ]
            assert buttons[-1].url == SUPPORT_URL
            assert "Выберите канал" in text if language == "ru" else "Choose the channel" in text
            assert not any(
                b.callback_data in ("account:home", "account:support", "fs:shop")
                for b in buttons
            )
        finally:
            _language.reset(token)


async def test_buyer_start_without_published_folders_keeps_original_catalog(monkeypatch):
    project = uuid4()
    folders = AsyncMock(return_value=())
    catalog = AsyncMock(return_value=True)
    monkeypatch.setattr(buyer_ui, "list_folders", folders)
    monkeypatch.setattr(buyer_ui, "_send_catalog", catalog)
    bot = MagicMock()
    bot.id = 700
    await buyer_ui._send_buyer_start(
        bot, chat_id=42, session_factory=MagicMock(), project_id=project,
    )
    folders.assert_awaited_once()
    assert folders.await_args.kwargs["bot_id"] == 700
    assert folders.await_args.kwargs["project_id"] == project
    catalog.assert_awaited_once()
    assert catalog.await_args.kwargs["chat_id"] == 42


async def test_buyer_start_with_published_folder_lists_active_channel_first(monkeypatch):
    project, folder_id = uuid4(), uuid4()
    monkeypatch.setattr(buyer_ui, "list_folders", AsyncMock(return_value=(
        SimpleNamespace(id=folder_id, name="Мои видео 🔥"),
    )))
    session = MagicMock()
    session.scalar = AsyncMock(return_value="Премиум канал ❤️")

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(buyer_ui, "system_project_session_scope", scope)
    legacy = AsyncMock()
    monkeypatch.setattr(buyer_ui, "_send_catalog", legacy)
    bot = MagicMock()
    bot.id = 700
    bot.send_message = AsyncMock()
    await buyer_ui._send_buyer_start(
        bot, chat_id=42, session_factory=MagicMock(), project_id=project,
    )
    legacy.assert_not_awaited()
    kwargs = bot.send_message.await_args.kwargs
    buttons = _buttons(kwargs["reply_markup"])
    assert [b.text for b in buttons] == [
        "Премиум канал ❤️", "Мои видео 🔥", "🌐 Language / Язык",
        "🆘 Поддержка",
    ]
    assert [b.callback_data for b in buttons] == [
        "buyer:channel", "fs:folder:" + encode_uuid(folder_id), "lng:menu",
        None,
    ]
    assert buttons[-1].url == SUPPORT_URL


async def test_subscription_button_uses_existing_catalog_and_denies_other_chats(monkeypatch):
    project = uuid4()
    router = buyer_ui.create_telegram_commerce_router(MagicMock(), project_id=project)
    callback_handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "buyer_channel_callback"
    )
    catalog = AsyncMock(return_value=True)
    monkeypatch.setattr(buyer_ui, "_send_catalog", catalog)
    bot = MagicMock()
    query = SimpleNamespace(
        data="buyer:channel",
        message=SimpleNamespace(
            message_id=901, chat=SimpleNamespace(id=42, type="private"),
        ),
        from_user=SimpleNamespace(id=42), answer=AsyncMock(),
    )
    await callback_handler(query, bot)
    catalog.assert_awaited_once()
    assert catalog.await_args.kwargs["message_id"] == 901
    assert catalog.await_args.kwargs["project_id"] == project
    query.message.chat.id = 400
    await callback_handler(query, bot)
    catalog.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_buyer_start_and_menu_route_dynamically_but_plans_remain_direct(monkeypatch):
    project = uuid4()
    router = buyer_ui.create_telegram_commerce_router(MagicMock(), project_id=project)
    handler = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "catalog_handler"
    )
    home, catalog = AsyncMock(), AsyncMock()
    monkeypatch.setattr(buyer_ui, "_send_buyer_start", home)
    monkeypatch.setattr(buyer_ui, "_send_catalog", catalog)
    monkeypatch.setattr(buyer_ui, "refresh_user_commands", AsyncMock())
    message = SimpleNamespace(
        text="/start", chat=SimpleNamespace(type="private", id=42),
        from_user=SimpleNamespace(id=42),
    )
    bot = MagicMock()
    for command in ("/start", "/menu"):
        message.text = command
        await handler(message, bot)
    assert home.await_count == 2
    catalog.assert_not_awaited()
    message.text = "/plans"
    await handler(message, bot)
    catalog.assert_awaited_once()

    # Aiogram Command matches media captions as well as normal text.
    message.text, message.caption = None, "/plans"
    await handler(message, bot)
    assert catalog.await_count == 2
    message.caption = "/start"
    await handler(message, bot)
    assert home.await_count == 3
    message.caption = None
    await handler(message, bot)
    assert home.await_count == 4
