"""Manual payment setup: Rich RU/EN prompts and precise one-step Back."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import SetupFlow


def _bot():
    bot = MagicMock()
    bot.id = 900
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=60))
    bot.delete_message = AsyncMock()
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def _query(action, *, message_id=9):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=123),
            message_id=message_id,
        ),
        answer=AsyncMock(),
    )


def _router(monkeypatch, project):
    actor_id = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    callback = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )
    reply = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "owner_reply_handler"
    )
    return callback, reply


def _buttons(kwargs):
    return [
        b.callback_data for row in kwargs["reply_markup"].inline_keyboard
        for b in row
    ]


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_method_currency_opens_one_rich_name_card_and_no_menu(
    monkeypatch, lang,
):
    project = uuid4()
    callback, _ = _router(monkeypatch, project)
    monkeypatch.setattr(ui, "owner_available_currencies", AsyncMock(
        return_value=("RUB", "USD"),
    ))
    monkeypatch.setattr(ui, "set_flow", AsyncMock(return_value="name-g"))
    monkeypatch.setattr(ui, "bind_prompt", AsyncMock())
    sender = AsyncMock(return_value=SimpleNamespace(message_id=61))
    monkeypatch.setattr(ui, "send_owner_rich", sender)
    bot = _bot()
    token = _language.set(lang)
    try:
        await callback(_query("methodcurrency:RUB"), bot)
    finally:
        _language.reset(token)
    screen = sender.await_args.kwargs
    assert "RUB" in screen["rich_html"]
    assert "<h3>" in screen["rich_html"]
    assert ("НОВЫЙ СПОСОБ ОПЛАТЫ" if lang == "ru"
            else "NEW PAYMENT METHOD") in screen["rich_html"]
    assert _buttons(screen) == ["own:promptback:name-g"]
    assert screen["reply_markup"].inline_keyboard[0][0].style == "primary"
    ui.set_flow.assert_awaited_once()
    assert ui.set_flow.await_args.kwargs["payload"] == {"currency": "RUB"}
    assert ui.bind_prompt.await_args.kwargs["message_id"] == 61
    # Retire currency chooser only after the replacement was sent.
    bot.delete_message.assert_awaited_once_with(123, 9)


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_name_input_replaces_same_card_with_rich_instruction(
    monkeypatch, lang,
):
    project = uuid4()
    _, reply = _router(monkeypatch, project)
    flow = SetupFlow(
        step="method_name", project_id=project, generation="name-g",
        payload={"currency": "RUB"}, prompt_message_id=61,
    )
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=flow))
    monkeypatch.setattr(ui, "advance_flow", AsyncMock(return_value="instructions-g"))
    monkeypatch.setattr(ui, "bind_prompt", AsyncMock())
    replace = AsyncMock(return_value=SimpleNamespace(message_id=61))
    monkeypatch.setattr(ui, "replace_owner_rich", replace)
    msg = SimpleNamespace(
        text="СБП", message_id=62,
        reply_to_message=SimpleNamespace(
            message_id=61, from_user=SimpleNamespace(id=900),
        ),
        chat=SimpleNamespace(type="private", id=123),
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
    )
    token = _language.set(lang)
    try:
        await reply(msg, _bot())
    finally:
        _language.reset(token)
    card = replace.await_args.kwargs
    assert card["message_id"] == 61
    assert "<h3>" in card["rich_html"]
    assert ("ИНСТРУКЦИЯ ДЛЯ ПОКУПАТЕЛЯ" if lang == "ru"
            else "BUYER PAYMENT INSTRUCTIONS") in card["rich_html"]
    assert "СБП" in card["rich_html"]
    for variable in ("{amount}", "{currency}", "{plan}", "{period}"):
        assert variable in card["rich_html"]
        assert variable in card["classic_html"]
    assert _buttons(card) == ["own:promptback:instructions-g"]
    assert ui.advance_flow.await_args.kwargs["payload"] == {
        "currency": "RUB", "name": "СБП",
    }
    assert ui.bind_prompt.await_args.kwargs["message_id"] == 61


@pytest.mark.parametrize("lang", ["ru", "en"])
async def test_instruction_back_restores_name_input_and_keeps_currency(
    monkeypatch, lang,
):
    project = uuid4()
    callback, _ = _router(monkeypatch, project)
    monkeypatch.setattr(ui, "cancel_draft", AsyncMock())
    monkeypatch.setattr(ui, "back_owner_flow", AsyncMock(return_value=SetupFlow(
        step="method_instruction", project_id=project,
        payload={"currency": "RUB", "name": "СБП"},
        prompt_message_id=61, generation="instructions-g",
    )))
    monkeypatch.setattr(ui, "set_flow", AsyncMock(return_value="new-name-g"))
    monkeypatch.setattr(ui, "bind_prompt", AsyncMock())
    replace = AsyncMock(return_value=SimpleNamespace(message_id=61))
    monkeypatch.setattr(ui, "replace_owner_rich", replace)
    token = _language.set(lang)
    try:
        await callback(_query("promptback:instructions-g", message_id=61), _bot())
    finally:
        _language.reset(token)
    assert ui.back_owner_flow.await_args.kwargs["expected_generation"] == "instructions-g"
    assert ui.set_flow.await_args.kwargs["step"] == "method_name"
    assert ui.set_flow.await_args.kwargs["payload"] == {"currency": "RUB"}
    card = replace.await_args.kwargs
    assert card["message_id"] == 61
    assert "СБП" in card["rich_html"]
    assert "RUB" in card["rich_html"]
    assert _buttons(card) == ["own:promptback:new-name-g"]


async def test_name_back_reopens_currency_picker_instead_of_methods(monkeypatch):
    project = uuid4()
    callback, _ = _router(monkeypatch, project)
    monkeypatch.setattr(ui, "cancel_draft", AsyncMock())
    monkeypatch.setattr(ui, "back_owner_flow", AsyncMock(return_value=SetupFlow(
        step="method_name", project_id=project,
        payload={"currency": "RUB"}, prompt_message_id=61,
        generation="name-g",
    )))
    monkeypatch.setattr(ui, "owner_available_currencies", AsyncMock(
        return_value=("RUB", "USD"),
    ))
    bot = _bot()
    await callback(_query("promptback:name-g", message_id=61), bot)
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert buttons == [
        "own:methodcurrency:RUB", "own:methodcurrency:USD",
        "own:currencyadd:method", "own:methods",
    ]
    assert bot.delete_message.await_args.args == (123, 61)


async def test_completion_replaces_instruction_with_one_success_list(monkeypatch):
    project = uuid4()
    _, reply = _router(monkeypatch, project)
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=SetupFlow(
        step="method_instruction", project_id=project,
        payload={"currency": "RUB", "name": "СБП"},
        prompt_message_id=61, generation="instructions-g",
    )))
    create = AsyncMock()
    monkeypatch.setattr(ui, "create_owner_payment_method", create)
    monkeypatch.setattr(ui, "list_owner_methods", AsyncMock(return_value=[]))
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=SimpleNamespace(
        bot_instances=(), sales_configured=False,
    )))
    replace = AsyncMock(return_value=SimpleNamespace(message_id=61))
    monkeypatch.setattr(ui, "replace_owner_rich", replace)
    msg = SimpleNamespace(
        text="Переведите {amount} {currency} по СБП",
        message_id=67,
        reply_to_message=SimpleNamespace(
            message_id=61, from_user=SimpleNamespace(id=900),
        ),
        chat=SimpleNamespace(type="private", id=123),
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
    )
    bot = _bot()
    await reply(msg, bot)
    create.assert_awaited_once()
    assert create.await_args.kwargs["name"] == "СБП"
    assert create.await_args.kwargs["currency_code"] == "RUB"
    assert replace.await_args.kwargs["message_id"] == 61
    assert "Способ оплаты добавлен" in replace.await_args.kwargs["rich_html"]
    bot.send_message.assert_not_awaited()
