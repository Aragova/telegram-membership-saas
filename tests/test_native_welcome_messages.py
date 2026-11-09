"""Telegram 2026 native welcome integration: tenant scope, rights and UX."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.owner_setup.welcome_messages import (
    WelcomeTarget,
    public_chat_url,
    telegram_chat_reference,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _bot():
    bot = MagicMock()
    bot.id = 991199
    bot.send_message = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(
        status="administrator", can_send_welcome_messages=True,
    ))
    return bot


def _query(data):
    return SimpleNamespace(
        data="own:" + data,
        from_user=SimpleNamespace(
            id=12345, username="owner", first_name="Owner",
        ),
        message=SimpleNamespace(chat=SimpleNamespace(id=12345, type="private")),
        answer=AsyncMock(),
    )


def _callback(factory, project_id):
    router = ui.create_owner_setup_router(factory, fixed_project_id=project_id)
    return next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )


def _init_auth(monkeypatch, project_id):
    actor_id = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=project_id),
    )
    return actor_id


def test_chat_identifier_safety_and_private_link_limitation():
    assert telegram_chat_reference("-100123456789") == -100123456789
    assert telegram_chat_reference("-998877") == -998877
    assert telegram_chat_reference("@publicchannel") == "@publicchannel"
    assert public_chat_url("@publicchannel") == "https://t.me/publicchannel"
    assert public_chat_url("-100123456789") is None
    for invalid in ("", "https://example.org", "@no/path", "123", "-x", "@a"):
        with pytest.raises(ValueError):
            telegram_chat_reference(invalid)
        assert public_chat_url(invalid) is None


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_welcome_listing_only_active_resources_and_read_only(
    monkeypatch, lang,
):
    project_id = uuid4()
    one, disabled = uuid4(), uuid4()
    _init_auth(monkeypatch, project_id)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=SetupOverview(
        project_id=project_id, project_name="Test", timezone="Europe/Moscow",
        bots=(), resources=(
            (one, "<Private> & Group", "active"),
            (disabled, "Archived", "disabled"),
        ), plans=(), methods=(),
    )))
    bot = _bot()
    handler = _callback(MagicMock(), project_id)
    language = _language.set(lang)
    try:
        await handler(_query("welcome"), bot)
    finally:
        _language.reset(language)
    sent = bot.send_message.await_args
    body = sent.args[1]
    assert ("Приветствен" if lang == "ru" else "Welcome") in body
    rows = sent.kwargs["reply_markup"].inline_keyboard
    callbacks = [x.callback_data for row in rows for x in row]
    assert "own:welcome:chat:" + encode_uuid(one) in callbacks
    assert all(encode_uuid(disabled) not in x for x in callbacks)
    assert "own:resources" in callbacks
    assert bot.get_chat_member.await_count == 0


async def test_welcome_target_permission_check_and_copy_template(monkeypatch):
    project_id, resource_id = uuid4(), uuid4()
    actor_id = _init_auth(monkeypatch, project_id)
    read = AsyncMock(return_value=WelcomeTarget(
        resource_id=resource_id, name="VIP <Channel> & more",
        external_id="@publicchannel", resource_type="telegram_channel",
    ))
    monkeypatch.setattr(ui, "read_welcome_target", read)
    bot = _bot()
    await _callback(MagicMock(), project_id)(
        _query("welcome:chat:" + encode_uuid(resource_id)), bot,
    )
    read.assert_awaited_once()
    assert read.await_args.kwargs["project_id"] == project_id
    assert read.await_args.kwargs["actor_id"] == actor_id
    bot.get_chat_member.assert_awaited_once_with(
        chat_id="@publicchannel", user_id=bot.id,
    )
    sent = bot.send_message.await_args
    body = sent.args[1]
    assert "VIP &lt;Channel&gt; &amp; more" in body
    assert "✅" in body
    assert "сохранено ли оно" in body
    rows = sent.kwargs["reply_markup"].inline_keyboard
    buttons = [btn for row in rows for btn in row]
    copy_buttons = [btn for btn in buttons if btn.copy_text is not None]
    assert len(copy_buttons) == 1
    assert "Добро пожаловать!" in copy_buttons[0].copy_text.text
    assert len(copy_buttons[0].copy_text.text) <= 256
    assert any(btn.url == "https://t.me/publicchannel" for btn in buttons)
    assert any(btn.callback_data == "own:welcome" for btn in buttons)


async def test_private_channel_missing_permission_does_not_claim_setup(monkeypatch):
    project_id, resource_id = uuid4(), uuid4()
    _init_auth(monkeypatch, project_id)
    monkeypatch.setattr(ui, "read_welcome_target", AsyncMock(
        return_value=WelcomeTarget(
            resource_id=resource_id, name="Private channel",
            external_id="-10012345", resource_type="telegram_channel",
        ),
    ))
    bot = _bot()
    bot.get_chat_member.return_value.can_send_welcome_messages = False
    await _callback(MagicMock(), project_id)(
        _query("welcome:chat:" + encode_uuid(resource_id)), bot,
    )
    sent = bot.send_message.await_args
    assert "🟡" in sent.args[1]
    assert "Telegram" in sent.args[1]
    buttons = [
        btn for row in sent.kwargs["reply_markup"].inline_keyboard for btn in row
    ]
    assert not any(btn.url for btn in buttons)


async def test_stale_or_cross_project_welcome_target_never_reaches_telegram(monkeypatch):
    project_id, resource_id = uuid4(), uuid4()
    _init_auth(monkeypatch, project_id)
    monkeypatch.setattr(ui, "read_welcome_target", AsyncMock(
        side_effect=ValueError("Channel unavailable in this bot"),
    ))
    bot = _bot()
    query = _query("welcome:chat:" + encode_uuid(resource_id))
    await _callback(MagicMock(), project_id)(query, bot)
    bot.get_chat_member.assert_not_awaited()
    assert "Channel unavailable" in str(bot.send_message.await_args)


async def test_no_customer_or_central_bot_can_open_owner_welcome_setup(monkeypatch):
    project_id = uuid4()
    _init_auth(monkeypatch, project_id)
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=False))
    bot = _bot()
    query = _query("welcome")
    await _callback(MagicMock(), project_id)(query, bot)
    query.answer.assert_awaited_once()
    assert "Нет доступа" in query.answer.await_args.args[0]
    bot.send_message.assert_not_awaited()
