"""Regression: owner wizard back paths must not leave stale steps or mutate sales."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import ReplyKeyboardRemove

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup import service
from telegram_membership_saas.modules.owner_setup.service import SetupFlow


def _bot():
    bot = MagicMock()
    bot.id = 900
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    return bot


def _query(action: str):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private"), message_id=10),
        from_user=SimpleNamespace(id=700, username="owner", first_name="Owner"),
        answer=AsyncMock(),
    )


def _callback(monkeypatch, project_id):
    actor_id = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=project_id),
    )
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project_id)
    callback = next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )
    return actor_id, callback


def _buttons(bot):
    keyboard = bot.send_message.await_args.kwargs["reply_markup"]
    return [
        (button.text, button.callback_data)
        for row in keyboard.inline_keyboard for button in row
    ]


@pytest.mark.asyncio
async def test_duration_back_cancels_only_current_wizard(monkeypatch):
    project_id, actor_id = uuid4(), uuid4()
    actor_id, callback = _callback(monkeypatch, project_id)
    flow = SetupFlow(
        step="plan_duration", project_id=project_id,
        payload={"name": "Premium"}, prompt_message_id=None,
        generation="current",
    )
    consume = AsyncMock(return_value=flow)
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "consume_button_flow", consume)
    monkeypatch.setattr(ui, "cancel_owner_flow", cancel)
    monkeypatch.setattr(
        ui, "setup_overview",
        AsyncMock(return_value=SimpleNamespace(plans=(), methods=())),
    )
    create = AsyncMock()
    monkeypatch.setattr(ui, "create_owner_plan", create)
    bot, query = _bot(), _query("flowback:plans:current")
    token = _language.set("en")
    try:
        await callback(query, bot)
    finally:
        _language.reset(token)
    consume.assert_awaited_once()
    assert consume.await_args.kwargs["expected_step"] == "plan_duration"
    assert consume.await_args.kwargs["generation"] == "current"
    assert consume.await_args.kwargs["bot_scope_id"] == 900
    assert consume.await_args.kwargs["discard"] is True
    cancel.assert_not_awaited()
    create.assert_not_awaited()
    assert "Plans" in bot.send_message.await_args.args[1]
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_resource_back_restores_duration_with_fresh_generation(monkeypatch):
    project_id = uuid4()
    _, callback = _callback(monkeypatch, project_id)
    flow = SetupFlow(
        step="plan_resource", project_id=project_id,
        payload={"name": "Premium", "value": 1, "unit": "month"},
        prompt_message_id=None, generation="old",
    )
    consume = AsyncMock(return_value=flow)
    advance = AsyncMock(return_value="new-generation")
    create = AsyncMock()
    monkeypatch.setattr(ui, "consume_button_flow", consume)
    monkeypatch.setattr(ui, "advance_flow", advance)
    monkeypatch.setattr(ui, "create_owner_plan", create)
    bot, query = _bot(), _query("flowback:duration:old")
    token = _language.set("en")
    try:
        await callback(query, bot)
        commands = _buttons(bot)
    finally:
        _language.reset(token)
    assert consume.await_args.kwargs["discard"] is False
    assert advance.await_args.kwargs["step"] == "plan_duration"
    assert advance.await_args.kwargs["payload"] == {"name": "Premium"}
    assert advance.await_args.kwargs["bot_scope_id"] == 900
    assert any(data == "own:duration:month:new-generation"
               for _, data in commands)
    assert any(data == "own:flowback:plans:new-generation"
               for _, data in commands)
    assert all(len(data.encode("utf-8")) <= 64 for _, data in commands)
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_back_cannot_cancel_newer_wizard(monkeypatch):
    project_id = uuid4()
    _, callback = _callback(monkeypatch, project_id)
    consume = AsyncMock(side_effect=ValueError("Шаг устарел. Откройте нужный раздел заново."))
    cancel = AsyncMock()
    advance = AsyncMock()
    monkeypatch.setattr(ui, "consume_button_flow", consume)
    monkeypatch.setattr(ui, "cancel_owner_flow", cancel)
    monkeypatch.setattr(ui, "advance_flow", advance)
    bot, query = _bot(), _query("flowback:plans:old")
    await callback(query, bot)
    assert query.answer.await_args.kwargs["show_alert"] is True
    cancel.assert_not_awaited()
    advance.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_owner_flow_atomically_invalidates_force_reply(monkeypatch):
    actor_id, project_id = uuid4(), uuid4()
    state = SimpleNamespace(
        step="price_amount", payload={"plan_id": uuid4().hex},
        selected_project_id=project_id, prompt_message_id=77,
        generation="g", expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=state))
    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session
    monkeypatch.setattr(service, "tenant_session_scope", scope)
    flow = await service.back_owner_flow(
        MagicMock(), actor_id=actor_id, bot_scope_id=900,
    )
    assert flow is not None
    assert flow.step == "price_amount"
    assert flow.payload["plan_id"]
    assert state.step is None
    assert state.payload == {}
    assert state.prompt_message_id is None
    assert state.generation is None
    assert state.selected_project_id == project_id
    assert session.get.await_args.args[1] == (actor_id, 900)
    assert session.get.await_args.kwargs["with_for_update"] is True


@pytest.mark.asyncio
async def test_back_command_routes_to_price_plan_without_modifying_price(monkeypatch):
    project_id, actor_id, plan_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    back = AsyncMock(return_value=SetupFlow(
        step="price_amount", project_id=project_id,
        payload={"plan_id": plan_id.hex}, prompt_message_id=55,
        generation="old",
    ))
    monkeypatch.setattr(ui, "back_owner_flow", back)
    monkeypatch.setattr(ui, "get_owner_plan_info", AsyncMock(return_value=SimpleNamespace(
        name="Premium", status="draft", duration_value=1,
        duration_unit="month", resource_name="Channel", prices=(),
    )))
    monkeypatch.setattr(ui, "owner_available_currencies", AsyncMock(return_value=("RUB",)))
    save = AsyncMock()
    monkeypatch.setattr(ui, "save_owner_price", save)
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project_id)
    handler = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "back_setup_message"
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=700),
        from_user=SimpleNamespace(id=700, username="owner", first_name="Owner"),
    )
    bot = _bot()
    await handler(message, bot)
    assert "Premium" in bot.send_message.await_args.args[1]
    assert any(x.endswith("own:plans") for _, x in _buttons(bot))
    assert back.await_args.kwargs["bot_scope_id"] == 900
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_price_validation_failure_has_one_actionable_back_button(monkeypatch):
    project_id, actor_id, plan_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    flow = SetupFlow(
        step="price_amount", project_id=project_id,
        payload={"plan_id": plan_id.hex, "currency": "RUB"},
        prompt_message_id=15, generation="g",
    )
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=flow))
    monkeypatch.setattr(
        ui, "save_owner_price",
        AsyncMock(side_effect=ValueError("Price amount must be finite and positive")),
    )
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project_id)
    handler = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "owner_reply_handler"
    )
    message = SimpleNamespace(
        text="0", message_id=20,
        chat=SimpleNamespace(type="private", id=700),
        from_user=SimpleNamespace(id=700, username="owner", first_name="Owner"),
        reply_to_message=SimpleNamespace(
            message_id=15, from_user=SimpleNamespace(id=900),
        ),
    )
    bot = _bot()
    token = _language.set("en")
    try:
        await handler(message, bot)
    finally:
        _language.reset(token)
    assert "positive" in bot.send_message.await_args.args[1]
    buttons = _buttons(bot)
    assert buttons == [("⬅️ Back to section", "own:plan:" + plan_id.hex)]


@pytest.mark.asyncio
async def test_method_currency_picker_has_back_and_no_extra_menu_commands(monkeypatch):
    project_id = uuid4()
    _, callback = _callback(monkeypatch, project_id)
    monkeypatch.setattr(ui, "owner_available_currencies", AsyncMock(return_value=("RUB", "USD")))
    bot, query = _bot(), _query("newmethod")
    await callback(query, bot)
    commands = _buttons(bot)
    assert ("⬅️ Payment methods", "own:methods") not in commands  # Russian default
    assert any(data == "own:methods" for _, data in commands)
    assert any(data == "own:methodcurrency:RUB" for _, data in commands)
    assert len(commands) == 3


@pytest.mark.asyncio
async def test_expired_chat_picker_back_removes_stale_telegram_keyboard(monkeypatch):
    """After the 15-minute TTL the expired step cannot identify a picker."""
    project_id, actor_id = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    pending = AsyncMock(return_value=None)
    monkeypatch.setattr(ui, "back_owner_flow", pending)
    monkeypatch.setattr(
        ui, "owner_home",
        AsyncMock(return_value=SimpleNamespace(selected=None, projects=())),
    )
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project_id)
    handler = next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == "back_setup_message"
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=700),
        from_user=SimpleNamespace(id=700, username="owner", first_name="Owner"),
    )
    bot = _bot()
    await handler(message, bot)
    assert pending.await_args.kwargs["bot_scope_id"] == 900
    assert bot.send_message.await_count == 2
    first, second = bot.send_message.await_args_list
    assert isinstance(first.kwargs["reply_markup"], ReplyKeyboardRemove)
    assert "главное меню" in first.args[1]
    assert "ViMi Pay" in second.args[1]


@pytest.mark.asyncio
async def test_generation_checked_and_discarded_under_single_transaction(monkeypatch):
    """No second unconditional cancellation may overwrite a newer setup."""
    actor_id, project_id = uuid4(), uuid4()
    state = SimpleNamespace(
        step="plan_duration", payload={"name": "Premium"},
        selected_project_id=project_id, prompt_message_id=None,
        generation="current", expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=state))
    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session
    monkeypatch.setattr(service, "tenant_session_scope", scope)
    result = await service.consume_button_flow(
        MagicMock(), actor_id=actor_id, bot_scope_id=900,
        expected_step="plan_duration", generation="current",
        discard=True,
    )
    assert result.generation == "current"
    assert state.step is None
    assert state.generation is None
    assert state.payload == {}
    assert state.expires_at <= datetime.now(UTC)

    # A different (newly started) wizard cannot be cleared with the old Back.
    state.step = "plan_name"
    state.generation = "new"
    state.payload = {"draft": "another"}
    state.expires_at = datetime.now(UTC) + timedelta(minutes=10)
    with pytest.raises(ValueError, match="Шаг устарел"):
        await service.consume_button_flow(
            MagicMock(), actor_id=actor_id, bot_scope_id=900,
            expected_step="plan_duration", generation="current",
            discard=True,
        )
    assert state.step == "plan_name"
    assert state.generation == "new"
    assert state.payload == {"draft": "another"}
