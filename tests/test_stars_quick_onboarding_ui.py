"""The existing Stars menu must finish onboarding without a tariff detour."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from test_v2_tenant_onboarding_ui import (
    _auth, _bot, _callback, _query, _snapshot,
)

from telegram_membership_saas.modules.owner_setup import handlers as ui


async def test_empty_stars_screen_guides_to_confirm_month_and_enter_price(monkeypatch):
    project = uuid4()
    _auth(monkeypatch, project)
    current = _snapshot(project, has_channel=True)
    monkeypatch.setattr(
        ui, "setup_overview", AsyncMock(return_value=current),
    )
    create = AsyncMock()
    monkeypatch.setattr(ui, "create_one_month_stars_plan", create)
    begin = AsyncMock(return_value="fresh-generation")
    binding = AsyncMock()
    monkeypatch.setattr(ui, "set_flow", begin)
    monkeypatch.setattr(ui, "bind_prompt", binding)
    bot = _bot()
    bot.send_message.return_value = SimpleNamespace(message_id=125)

    await _callback(project)(_query("starspage:0"), bot)
    page = bot.send_message.await_args
    assert "1 месяц" in page.args[1]
    assert "own:starsnew" in {
        b.callback_data for row in page.kwargs["reply_markup"].inline_keyboard
        for b in row
    }
    await _callback(project)(_query("starsnew"), bot)
    warning = bot.send_message.await_args
    assert "До ввода цены ничего не создаётся" in warning.args[1]
    assert "own:starsnewok" in {
        b.callback_data for row in warning.kwargs["reply_markup"].inline_keyboard
        for b in row
    }
    assert create.await_count == 0

    await _callback(project)(_query("starsnewok"), bot)
    pricing = bot.send_message.await_args
    assert "Введите цену" in pricing.args[1]
    assert begin.await_args.kwargs["step"] == "stars_month_price"
    assert begin.await_args.kwargs["payload"]["resource_id"] == (
        current.resources[0][0].hex
    )
    assert binding.await_count == 1
    assert create.await_count == 0


async def test_multiple_channels_prompt_selection_and_native_stars_readiness(monkeypatch):
    project, first, second = uuid4(), uuid4(), uuid4()
    _auth(monkeypatch, project)
    snapshot = replace(
        _snapshot(project, has_channel=True),
        resources=((first, "Channel 1", "active"),
                   (second, "Channel 2", "active")),
    )
    monkeypatch.setattr(
        ui, "setup_overview", AsyncMock(return_value=snapshot),
    )
    bot = _bot()
    await _callback(project)(_query("starsnewok"), bot)
    callbacks = [
        b.callback_data
        for row in bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard for b in row
    ]
    assert "own:starsnewres:" + first.hex in callbacks
    assert "own:starsnewres:" + second.hex in callbacks

    native = replace(
        snapshot, plans=((uuid4(), "1 месяц", "active"),),
        methods=(), sales_configured=True,
        live_bot_username="stars_channel_bot",
    )
    assert ui._next_setup_step(native) is None
    assert any(
        line.startswith("✅") and "Способ оплаты" in line
        for line in ui._progress(native)
    )
