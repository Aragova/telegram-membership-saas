"""One contextual sales toggle per list; guarded owner confirmation RU/EN."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.bulk_sales import BulkSalesState
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.owner_setup.tenant_admin import MethodInfo
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _bot():
    bot = MagicMock()
    bot.id = 500
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=10))
    return bot


def _query(action):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private"), message_id=5),
        from_user=SimpleNamespace(id=12345, first_name="Owner", username="owner"),
        answer=AsyncMock(),
    )


def _buttons(bot):
    return [
        b.callback_data
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row if b.callback_data
    ]


def _handler(monkeypatch, project, instance):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=SetupOverview(
        project_id=project, project_name="Example",
        timezone="Europe/Moscow", bots=(("example_bot", "registered"),),
        resources=(), plans=((instance, "Monthly", "active"),),
        methods=((instance, "Bank", "active"),),
    )))
    monkeypatch.setattr(ui, "list_owner_methods", AsyncMock(
        return_value=(MethodInfo(instance, "Bank", "active", ("RUB",)),),
    ))
    cb = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    return next(
        h.callback for h in cb.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )


@pytest.mark.parametrize("kind,route", [("plan", "plans"), ("method", "methods")])
async def test_bulk_button_confirm_is_one_dynamic_control(monkeypatch, kind, route):
    project, instance = uuid4(), uuid4()
    cb = _handler(monkeypatch, project, instance)
    state = BulkSalesState(kind=kind, active=2, paused=1, drafts=1,
                           revision="0123456789ab")
    state_mock = AsyncMock(return_value=state)
    mutate = AsyncMock(return_value=2)
    monkeypatch.setattr(ui, "read_bulk_sales_state", state_mock)
    monkeypatch.setattr(ui, "switch_all_sales", mutate)
    bot = _bot()
    token = _language.set("ru")
    try:
        await cb(_query(route), bot)
        buttons = _buttons(bot)
        expected = "own:bulks:" + kind + ":off:" + encode_uuid(project)
        assert buttons.count(expected) == 1
        assert not any("bulks:" + kind + ":on:" in x for x in buttons)
        assert all(len(x.encode()) <= 64 for x in buttons)
        await cb(_query(expected.removeprefix("own:")), bot)
        confirm = "own:bulkok:" + kind + ":off:" + encode_uuid(project)
        confirm += ":" + state.revision
        assert confirm in _buttons(bot)
        assert "подписки" in bot.send_message.await_args.args[1]
        mutate.assert_not_awaited()
        await cb(_query(confirm.removeprefix("own:")), bot)
        mutate.assert_awaited_once()
        assert mutate.await_args.kwargs["project_id"] == project
        assert mutate.await_args.kwargs["enable"] is False
        assert mutate.await_args.kwargs["expected_revision"] == state.revision
    finally:
        _language.reset(token)


async def test_new_button_switches_to_enable_when_everything_off(monkeypatch):
    project, instance = uuid4(), uuid4()
    cb = _handler(monkeypatch, project, instance)
    monkeypatch.setattr(ui, "read_bulk_sales_state", AsyncMock(
        return_value=BulkSalesState("plan", 0, 5, 2, "aaaabbbbcccc"),
    ))
    bot = _bot()
    lang = _language.set("en")
    try:
        await cb(_query("plans"), bot)
        assert "own:bulks:plan:on:" + encode_uuid(project) in _buttons(bot)
        assert "own:bulks:plan:off:" + encode_uuid(project) not in _buttons(bot)
        await cb(_query("bulks:plan:on:" + encode_uuid(project)), bot)
        assert "Drafts are never published automatically" in (
            bot.send_message.await_args.args[1]
        )
    finally:
        _language.reset(lang)


async def test_bulk_callback_rejects_other_project_and_cannot_access_registry(monkeypatch):
    project, instance, foreign = uuid4(), uuid4(), uuid4()
    cb = _handler(monkeypatch, project, instance)
    change = AsyncMock(return_value=1)
    monkeypatch.setattr(ui, "switch_all_sales", change)
    query = _query(
        "bulkok:plan:off:" + encode_uuid(foreign) + ":aaaabbbbcccc"
    )
    await cb(query, _bot())
    change.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True

    central = ui.create_owner_setup_router(MagicMock(), fixed_project_id=None)
    central_callback = next(
        x.callback for x in central.callback_query.handlers
        if x.callback.__name__ == "owner_callback"
    )
    another = _query("bulkok:plan:off:" + encode_uuid(project) + ":aaaabbbbcccc")
    await central_callback(another, _bot())
    change.assert_not_awaited()
    assert another.answer.await_args.kwargs["show_alert"] is True
