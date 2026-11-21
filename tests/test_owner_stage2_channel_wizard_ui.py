"""Stage 2/4 contracts: real Telegram picker, one Back and RU/EN Rich cards."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import ReplyKeyboardRemove

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.channel_stage2_ui import (
    directory_classic,
    directory_rich,
    manual_classic,
    manual_rich,
    picker_classic,
)
from telegram_membership_saas.modules.owner_setup.service import SetupFlow, SetupOverview
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER


@pytest.mark.parametrize(("language", "title", "add", "group", "back"), [
    ("ru", "Мои каналы и группы", "Добавить канал", "Добавить группу", "Назад"),
    ("en", "My channels and groups", "Add channel", "Add group", "Back"),
])
def test_directory_and_manual_rich_classic_language_contract(
    language, title, add, group, back,
):
    token = _language.set(language)
    try:
        empty = directory_rich(0)
        basic = directory_classic(0)
        manual = manual_rich("@vimitest21bot")
        manual_basic = manual_classic("@vimitest21bot")
        other = directory_rich(3)
    finally:
        _language.reset(token)
    assert title in empty and title in basic
    assert "<table bordered compact>" in empty
    assert "0</td>" in empty
    assert "3</td>" in other
    assert "dashboard" not in empty.lower()
    assert ("полная панель" if language == "ru" else "full management") in empty
    assert ("полная панель" if language == "ru" else "full management") not in other
    assert RICH_ACTIONS_MARKER in empty
    assert RICH_ACTIONS_MARKER not in basic
    for html in (manual, manual_basic):
        assert "@vimitest21bot" in html
        assert "-1001234567890" in html and "@my_private_channel" in html
        assert "2/4" in html or "2 of 4" in html
        assert ("блокировки" if language == "ru" else "ban") in html
        assert "В меню" not in html and "Main menu" not in html
    assert RICH_ACTIONS_MARKER in manual
    assert RICH_ACTIONS_MARKER not in manual_basic
    assert add and group and back


@pytest.mark.parametrize(("language", "title", "group_title", "select"), [
    ("ru", "ПОДКЛЮЧЕНИЕ КАНАЛА", "ПОДКЛЮЧЕНИЕ ГРУППЫ", "Выберите"),
    ("en", "CONNECT A CHANNEL", "CONNECT A GROUP", "Choose"),
])
def test_picker_is_html_safe_in_both_languages(language, title, group_title, select):
    token = _language.set(language)
    try:
        channel = picker_classic("channel", "@vimi<&bot")
        group = picker_classic("group", "@vimi<&bot")
    finally:
        _language.reset(token)
    assert title in channel and group_title in group
    for text in (channel, group):
        assert "@vimi&lt;&amp;bot" in text
        assert "@vimi<&bot" not in text
        assert "2" in text and "4" in text
        assert select in text
        assert "dashboard" not in text.lower()
    with pytest.raises(ValueError):
        picker_classic("unsupported", "bot")


def _bot():
    bot = MagicMock()
    bot.id = 987
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=111))
    return bot


def _query(action: str):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(id=345, username="owner", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=345)),
        answer=AsyncMock(),
    )


def _message(text: str):
    return SimpleNamespace(
        chat=SimpleNamespace(type="private", id=345),
        from_user=SimpleNamespace(id=345, username="owner", first_name="Owner"),
        text=text,
    )


def _handlers(project):
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    callback = next(x.callback for x in router.callback_query.handlers
                    if x.callback.__name__ == "owner_callback")
    back = next(x.callback for x in router.message.handlers
                if x.callback.__name__ == "back_native_chat_picker")
    return callback, back


def _setup(project, count=0):
    return SetupOverview(
        project_id=project, project_name="ViMi channel",
        timezone="Europe/Moscow", bots=(("vimitest21bot", "registered"),),
        plans=(), methods=(), bot_instances=((uuid4(), "vimitest21bot", "registered"),),
        resources=tuple((uuid4(), "Channel", "active") for _ in range(count)),
    )


def _auth(monkeypatch, project):
    monkeypatch.setattr(ui, "ensure_telegram_actor",
                        AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "get_selected_project",
                        AsyncMock(return_value=project))
    monkeypatch.setattr(ui, "setup_overview",
                        AsyncMock(return_value=_setup(project)))


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_directory_has_no_dashboard_shortcut_and_back_goes_to_stage_one(
    monkeypatch, language,
):
    project = uuid4()
    _auth(monkeypatch, project)
    render = AsyncMock(return_value=SimpleNamespace(message_id=111))
    monkeypatch.setattr(ui, "send_owner_rich", render)
    handler, _ = _handlers(project)
    bot = _bot()
    token = _language.set(language)
    try:
        await handler(_query("resources"), bot)
    finally:
        _language.reset(token)
    card = render.await_args.kwargs
    actions = [(btn.text, btn.callback_data) for row in
               card["reply_markup"].inline_keyboard for btn in row]
    assert len(actions) == 4
    assert [value for _, value in actions] == [
        "own:pick:channel", "own:pick:group",
        "own:resource:manual", "own:dashboard",
    ]
    assert actions[-1][0] == ("⬅️ Назад" if language == "ru" else "⬅️ Back")
    assert all("admin" not in str(x).lower() for x in actions)
    assert "0</td>" in card["rich_html"]


@pytest.mark.parametrize(("language", "kind", "label", "is_channel"), [
    ("ru", "channel", "📢 Выбрать канал", True),
    ("ru", "group", "👥 Выбрать группу", False),
    ("en", "channel", "📢 Select channel", True),
    ("en", "group", "👥 Select group", False),
])
async def test_native_telegram_picker_preserves_request_chat_and_has_only_back(
    monkeypatch, language, kind, label, is_channel,
):
    project = uuid4()
    _auth(monkeypatch, project)
    begin = AsyncMock(return_value="new-gen")
    monkeypatch.setattr(ui, "set_flow", begin)
    handler, _ = _handlers(project)
    bot = _bot()
    token = _language.set(language)
    try:
        await handler(_query("pick:" + kind), bot)
    finally:
        _language.reset(token)
    sent = bot.send_message.await_args
    assert sent.kwargs["parse_mode"] == "HTML"
    assert "@vimitest21bot" in sent.args[1]
    assert "2" in sent.args[1]
    keyboard = sent.kwargs["reply_markup"].keyboard
    assert len(keyboard) == 2 and all(len(row) == 1 for row in keyboard)
    assert keyboard[0][0].text == label
    assert keyboard[1][0].text == ("⬅️ Назад" if language == "ru" else "⬅️ Back")
    picker = keyboard[0][0].request_chat
    assert picker is not None
    assert picker.chat_is_channel is is_channel
    assert picker.request_title is True
    assert begin.await_args.kwargs["step"] == "chat_picker"
    assert begin.await_args.kwargs["payload"]["request_id"] == picker.request_id
    assert begin.await_args.kwargs["bot_scope_id"] == 987


async def test_back_from_native_picker_disposes_only_current_flow(monkeypatch):
    project = uuid4()
    _auth(monkeypatch, project)
    flow = SetupFlow(
        step="chat_picker", project_id=project, payload={"request_id": 42},
        prompt_message_id=None, generation="native-gen",
    )
    consumed = AsyncMock(return_value=flow)
    monkeypatch.setattr(ui, "consume_button_flow", consumed)
    render = AsyncMock(return_value=SimpleNamespace(message_id=111))
    monkeypatch.setattr(ui, "send_owner_rich", render)
    _, back = _handlers(project)
    bot = _bot()
    await back(_message("⬅️ Назад"), bot)
    consumed.assert_awaited_once()
    assert consumed.await_args.kwargs["expected_step"] == "chat_picker"
    assert consumed.await_args.kwargs["discard"] is True
    assert consumed.await_args.kwargs["bot_scope_id"] == 987
    assert isinstance(
        bot.send_message.await_args.kwargs["reply_markup"], ReplyKeyboardRemove,
    )
    assert render.await_count == 1
    assert [b.callback_data for row in
            render.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row][-1] == "own:dashboard"


async def test_stale_native_back_does_not_cancel_newer_form(monkeypatch):
    project = uuid4()
    _auth(monkeypatch, project)
    monkeypatch.setattr(ui, "consume_button_flow",
                        AsyncMock(side_effect=ValueError("expired")))
    monkeypatch.setattr(ui, "send_owner_rich", AsyncMock())
    _, back = _handlers(project)
    bot = _bot()
    await back(_message("⬅️ Back"), bot)
    assert "⚠️" in bot.send_message.await_args.args[1]
    assert bot.send_message.await_count == 1
    ui.send_owner_rich.assert_not_awaited()


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_manual_input_rich_and_single_back_button(monkeypatch, language):
    project = uuid4()
    _auth(monkeypatch, project)
    monkeypatch.setattr(ui, "set_flow",
                        AsyncMock(return_value="active-gen"))
    binding = AsyncMock()
    monkeypatch.setattr(ui, "bind_prompt", binding)
    render = AsyncMock(return_value=SimpleNamespace(message_id=777))
    monkeypatch.setattr(ui, "send_owner_rich", render)
    handler, _ = _handlers(project)
    token = _language.set(language)
    try:
        await handler(_query("resource:manual"), _bot())
    finally:
        _language.reset(token)
    fields = render.await_args.kwargs
    assert "2/4" in fields["rich_html"]
    assert "@vimitest21bot" in fields["rich_html"]
    assert "-1001234567890" in fields["classic_html"]
    actions = [btn.callback_data for row in
               fields["reply_markup"].inline_keyboard for btn in row]
    assert actions == ["own:promptback:active-gen"]
    assert binding.await_args.kwargs["message_id"] == 777
    assert binding.await_args.kwargs["step"] == "resource_reference"
