"""Regression coverage for one-message channel outcomes and step 3 transition."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import ReplyKeyboardRemove

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.channel_stage2_ui import (
    connection_result_classic,
    connection_result_rich,
    directory_rich,
    manual_rich,
)
from telegram_membership_saas.modules.owner_setup.onboarding_ui import (
    owner_setup_step_rich,
)
from telegram_membership_saas.modules.owner_setup.service import SetupFlow, SetupOverview


def _data(project, resources=()):
    return SetupOverview(
        project_id=project, project_name="ViMi Pay",
        timezone="Europe/Moscow",
        bots=(("vimitest21bot", "registered"),),
        bot_instances=((uuid4(), "vimitest21bot", "registered"),),
        resources=tuple((uuid4(), name, "active") for name in resources),
        methods=(), plans=(), sales_configured=False,
    )


def _bot():
    bot = MagicMock()
    bot.id = 987
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=450))
    bot.edit_message_reply_markup = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


def _message(*, text=None, chat_shared=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=345, username="owner", first_name="Owner"),
        chat=SimpleNamespace(type="private", id=345),
        text=text, chat_shared=chat_shared, message_id=223,
        reply_to_message=None,
    )


def _setup(monkeypatch, project, *, resources=(), first_run=True):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(
        ui, "setup_overview", AsyncMock(return_value=_data(project, resources)),
    )
    monkeypatch.setattr(
        ui, "saas_price_profile",
        AsyncMock(return_value={"currency_selected": not first_run}),
    )


def _handler(project, name):
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    all_handlers = (list(router.message.handlers)
                    + list(router.callback_query.handlers))
    return next(x.callback for x in all_handlers if x.callback.__name__ == name)


@pytest.mark.parametrize("language", ["ru", "en"])
async def test_first_run_catalog_hides_existing_channel_stats_buttons(
    monkeypatch, language,
):
    project = uuid4()
    _setup(monkeypatch, project, resources=("Test channel", "Test group"))
    render = AsyncMock(return_value=SimpleNamespace(message_id=200))
    monkeypatch.setattr(ui, "send_owner_rich", render)
    query = SimpleNamespace(
        data="own:resources",
        from_user=SimpleNamespace(id=345, username="owner", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=345)),
        answer=AsyncMock(),
    )
    token = _language.set(language)
    try:
        await _handler(project, "owner_callback")(query, _bot())
    finally:
        _language.reset(token)
    args = render.await_args.kwargs
    assert "2</td>" in args["rich_html"]
    actions = [item.callback_data for row in
               args["reply_markup"].inline_keyboard for item in row]
    assert actions == [
        "own:pick:channel", "own:pick:group",
        "own:resource:manual", "own:dashboard",
    ]
    assert not any(action.startswith("own:resourceview:") for action in actions)


async def test_finished_project_keeps_existing_resource_management(monkeypatch):
    project = uuid4()
    _setup(monkeypatch, project, resources=("Existing channel",), first_run=False)
    render = AsyncMock(return_value=SimpleNamespace(message_id=200))
    monkeypatch.setattr(ui, "send_owner_rich", render)
    query = SimpleNamespace(
        data="own:resources",
        from_user=SimpleNamespace(id=345, username="owner", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=345)),
        answer=AsyncMock(),
    )
    await _handler(project, "owner_callback")(query, _bot())
    actions = [item.callback_data for row in
               render.await_args.kwargs["reply_markup"].inline_keyboard for item in row]
    assert actions[0].startswith("own:resourceview:")


@pytest.mark.parametrize("language,kind", [
    ("ru", "channel"), ("ru", "group"),
    ("en", "channel"), ("en", "group"),
])
@pytest.mark.parametrize("success", [True, False])
async def test_native_picker_has_one_result_and_one_inline_action(
    monkeypatch, language, kind, success,
):
    project = uuid4()
    _setup(monkeypatch, project, resources=("Already connected",) if success else ())
    consumed = AsyncMock(return_value=SetupFlow(
        project_id=project, step="chat_picker",
        payload={"request_id": 19, "kind": kind}, generation="native",
        prompt_message_id=None,
    ))
    monkeypatch.setattr(ui, "consume_button_flow", consumed)
    verify = AsyncMock(
        return_value=SimpleNamespace(
            name="Testing <VIP>", resource_type="telegram_" + kind,
        ),
    )
    if not success:
        verify.side_effect = ValueError("Bot is not an administrator")
    monkeypatch.setattr(ui, "verify_channel_and_add", verify)
    render = AsyncMock()
    monkeypatch.setattr(ui, "send_owner_rich", render)
    bot = _bot()
    event = _message(
        chat_shared=SimpleNamespace(request_id=19, chat_id=-1001234567890),
    )
    token = _language.set(language)
    try:
        await _handler(project, "shared_chat_handler")(event, bot)
    finally:
        _language.reset(token)
    assert bot.send_message.await_count == 1
    sent = bot.send_message.await_args
    assert isinstance(sent.kwargs["reply_markup"], ReplyKeyboardRemove)
    assert ("✅" if success else "⚠️") in sent.args[1]
    thing = (
        ("группа" if kind == "group" else "канал") if language == "ru"
        else ("group" if kind == "group" else "channel")
    )
    assert thing in sent.args[1].lower()
    assert bot.edit_message_reply_markup.await_count == 1
    edited = bot.edit_message_reply_markup.await_args.kwargs
    assert edited["message_id"] == 450
    buttons = edited["reply_markup"].inline_keyboard
    assert len(buttons) == 1 and len(buttons[0]) == 1
    assert buttons[0][0].callback_data == (
        "own:dashboard" if success else "own:resources"
    )
    assert consumed.await_args.kwargs["discard"] is True
    assert bot.delete_message.await_count == 1
    render.assert_not_awaited()  # Never send a second directory card.


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("success", [True, False])
async def test_manual_id_one_rich_result_and_correct_navigation(
    monkeypatch, language, success,
):
    project = uuid4()
    _setup(monkeypatch, project, resources=("new group",) if success else ())
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=SetupFlow(
        step="resource_reference", project_id=project,
        payload={}, generation="prompt", prompt_message_id=120,
    )))
    verify = AsyncMock(return_value=SimpleNamespace(
        name="New group", resource_type="telegram_group",
    ))
    if not success:
        verify.side_effect = ValueError("No access")
    monkeypatch.setattr(ui, "verify_channel_and_add", verify)
    render = AsyncMock(return_value=SimpleNamespace(message_id=777))
    monkeypatch.setattr(ui, "send_owner_rich", render)
    bot = _bot()
    token = _language.set(language)
    try:
        await _handler(project, "owner_reply_handler")(
            _message(text="-1001234567890"), bot,
        )
    finally:
        _language.reset(token)
    assert render.await_count == 1
    assert bot.send_message.await_count == 0
    args = render.await_args.kwargs
    assert "<h3>" in args["rich_html"]
    assert "✅" in args["rich_html"] if success else "⚠️" in args["rich_html"]
    assert [item.callback_data for row in
            args["reply_markup"].inline_keyboard for item in row] == [
        "own:dashboard" if success else "own:resources",
    ]


@pytest.mark.parametrize("language", ["ru", "en"])
def test_structured_manual_card_and_step_three(language):
    token = _language.set(language)
    try:
        card = manual_rich("vimitest21bot")
        after = owner_setup_step_rich(
            bot_name="vimitest21bot", timezone="Europe/Moscow",
            action="methods",
        )
        failed = connection_result_rich(
            success=False, kind="group", username="vimitest21bot",
        )
        fallback = connection_result_classic(
            success=True, kind="group", username="vimitest21bot",
            resource_name="<private>", onboarding=True,
        )
        intro = directory_rich(2, onboarding=True)
    finally:
        _language.reset(token)
    assert card.count("<h4>") >= 3
    assert "<ul>" in card and "<pre>" in card
    assert "@vimitest21bot" in card
    assert "3/4" in after
    assert "4/4" not in after
    assert "⚠️" in failed and "1️⃣" in failed
    assert "&lt;private&gt;" in fallback
    assert "2</td>" in intro
