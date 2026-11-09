"""Owner plan management stays in the plan card, not the native Menu."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import SetupFlow, SetupOverview
from telegram_membership_saas.modules.owner_setup.tenant_admin import PlanInfo
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _bot():
    bot = MagicMock()
    bot.id = 333333
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=100))
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _buttons(bot):
    return [
        (b.text, b.callback_data)
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]


def _info(plan_id, *, status="draft", history=False, purchased=None):
    return PlanInfo(
        id=plan_id, name="Existing <Premium>",
        status=status, duration_value=1, duration_unit="month",
        resource_name="My channel", prices=(), has_history=history,
        has_purchase=history if purchased is None else purchased,
        revision="abcd1234abcd",
    )


def _overview(project_id):
    return SetupOverview(
        project_id=project_id, project_name="Project",
        timezone="Europe/Moscow", bots=(("owner_bot", "registered"),),
        resources=((uuid4(), "Channel", "active"),),
        plans=((uuid4(), "Existing", "active"),),
        methods=((uuid4(), "Bank", "active"),),
        sales_configured=True,
    )


def _callback(monkeypatch, project, *, info):
    actor = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=project),
    )
    monkeypatch.setattr(
        ui, "get_owner_plan_info", AsyncMock(return_value=info),
    )
    monkeypatch.setattr(
        ui, "owner_available_currencies", AsyncMock(return_value=("RUB", "USD")),
    )
    monkeypatch.setattr(
        ui, "setup_overview", AsyncMock(return_value=_overview(project)),
    )
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    callback = next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "owner_callback"
    )
    return callback


def _query(action):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private"), message_id=900),
        from_user=SimpleNamespace(
            id=12345, username="owner", first_name="Owner",
        ),
        answer=AsyncMock(),
    )


@pytest.mark.parametrize(
    ("status", "history", "expected", "excluded"),
    [
        (
            "draft", False,
            ("plrename:", "plduration:", "plresource:", "publish:", "pldelete:"),
            ("plarchive:", "plrestore:"),
        ),
        (
            "active", False, ("plrename:", "plarchive:", "pldelete:"),
            ("plduration:", "plresource:", "plrestore:"),
        ),
        (
            "archived", True, ("plrename:", "plrestore:"),
            ("plduration:", "plresource:", "plarchive:", "pldelete:"),
        ),
    ],
)
async def test_plan_card_has_contextual_management(
    monkeypatch, status, history, expected, excluded,
):
    project, plan = uuid4(), uuid4()
    cb = _callback(monkeypatch, project, info=_info(
        plan, status=status, history=history,
    ))
    bot = _bot()
    token = _language.set("en")
    try:
        await cb(_query("plan:" + plan.hex), bot)
        message = bot.send_message.await_args.args[1]
        buttons = _buttons(bot)
    finally:
        _language.reset(token)
    assert "Existing &lt;Premium&gt;" in message
    assert "Manage plan" in message
    callbacks = [action for _, action in buttons]
    assert all(any(item.startswith("own:" + expected_item) for item in callbacks)
               for expected_item in expected)
    assert all(not any(item.startswith("own:" + excluded_item)
                       for item in callbacks) for excluded_item in excluded)
    assert all(len(action.encode("utf-8")) <= 64 for action in callbacks)
    assert callbacks[-1] == "own:plans"


async def test_deleting_plan_requires_confirmation_and_expected_status(monkeypatch):
    project, plan = uuid4(), uuid4()
    cb = _callback(monkeypatch, project, info=_info(plan, status="draft"))
    delete = AsyncMock(return_value="Existing")
    monkeypatch.setattr(ui, "delete_unused_owner_plan", delete)
    bot = _bot()
    query = _query("pldelete:" + encode_uuid(plan))
    await cb(query, bot)
    delete.assert_not_awaited()
    assert "🗑" in bot.send_message.await_args.args[1]
    buttons = _buttons(bot)
    confirm = [v for _, v in buttons if v.startswith("own:pldelok:")]
    assert confirm == [
        "own:pldelok:" + encode_uuid(plan) + ":d:abcd1234abcd",
    ]
    assert any(v == "own:plan:" + plan.hex for _, v in buttons)
    query.data = confirm[0]
    await cb(query, bot)
    delete.assert_awaited_once()
    assert delete.await_args.kwargs["expected_status"] == "draft"
    assert delete.await_args.kwargs["expected_revision"] == "abcd1234abcd"
    assert delete.await_args.kwargs["plan_id"] == plan
    assert bot.send_message.await_args.args[1].startswith("📦")
    query.answer.assert_awaited()


async def test_archive_requires_confirm_and_preserves_history(monkeypatch):
    project, plan = uuid4(), uuid4()
    cb = _callback(monkeypatch, project, info=_info(
        plan, status="active", history=True,
    ))
    archive = AsyncMock(return_value="Existing")
    monkeypatch.setattr(ui, "archive_owner_plan", archive)
    bot = _bot()
    query = _query("plarchive:" + encode_uuid(plan))
    await cb(query, bot)
    archive.assert_not_awaited()
    assert any(x.startswith("own:plarchok:") for _, x in _buttons(bot))
    assert "Существующие подписки" in bot.send_message.await_args.args[1]
    query.data = "own:plarchok:" + encode_uuid(plan) + ":abcd1234abcd"
    await cb(query, bot)
    archive.assert_awaited_once()
    assert archive.await_args.kwargs["project_id"] == project
    assert archive.await_args.kwargs["expected_revision"] == "abcd1234abcd"


async def test_rename_prompt_and_completed_reply_return_to_plan(monkeypatch):
    project, plan = uuid4(), uuid4()
    cb = _callback(monkeypatch, project, info=_info(plan, status="active"))
    set_flow = AsyncMock(return_value="generation")
    bind = AsyncMock()
    monkeypatch.setattr(ui, "set_flow", set_flow)
    monkeypatch.setattr(ui, "bind_prompt", bind)
    bot = _bot()
    await cb(_query("plrename:" + encode_uuid(plan)), bot)
    assert set_flow.await_args.kwargs["step"] == "plan_rename"
    assert set_flow.await_args.kwargs["payload"]["plan_id"] == plan.hex
    bind.assert_awaited_once()
    assert "2–120" in bot.send_message.await_args.args[1]

    flow = SetupFlow(
        step="plan_rename", project_id=project,
        payload={"plan_id": plan.hex}, prompt_message_id=100,
        generation="generation",
    )
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=flow))
    rename = AsyncMock(return_value="New plan")
    monkeypatch.setattr(ui, "rename_owner_plan", rename)
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    reply = next(
        entry.callback for entry in router.message.handlers
        if entry.callback.__name__ == "owner_reply_handler"
    )
    message = SimpleNamespace(
        text="New plan", message_id=101,
        chat=SimpleNamespace(type="private", id=12345),
        from_user=SimpleNamespace(id=12345, username="owner", first_name="Owner"),
        reply_to_message=SimpleNamespace(
            message_id=100, from_user=SimpleNamespace(id=bot.id),
        ),
    )
    await reply(message, bot)
    rename.assert_awaited_once()
    assert rename.await_args.kwargs["name"] == "New plan"
    assert bot.send_message.await_args.args[1].startswith("📦")


async def test_draft_resource_and_duration_editors_only_show_safe_options(monkeypatch):
    project, plan = uuid4(), uuid4()
    cb = _callback(monkeypatch, project, info=_info(plan))
    bot = _bot()
    await cb(_query("plduration:" + encode_uuid(plan)), bot)
    durations = _buttons(bot)
    assert any(x.endswith(":month") for _, x in durations)
    assert any(x.endswith(":lifetime") for _, x in durations)
    assert len(durations) == 7
    await cb(_query("plresource:" + encode_uuid(plan)), bot)
    channels = _buttons(bot)
    assert len(channels) == 2
    assert channels[0][1].startswith("own:plsetres:")
    assert len(channels[0][1].encode("utf-8")) <= 64
    assert channels[-1][1] == "own:plan:" + plan.hex


async def test_paid_plan_does_not_offer_permanent_delete(monkeypatch):
    project, plan = uuid4(), uuid4()
    cb = _callback(monkeypatch, project, info=_info(
        plan, status="active", history=True,
    ))
    bot = _bot()
    await cb(_query("plan:" + plan.hex), bot)
    assert "Удаление недоступно" in bot.send_message.await_args.args[1]
    assert not any(data.startswith("own:pldelete:")
                   for _, data in _buttons(bot))


async def test_unpaid_invoice_keeps_delete_button_and_preserves_history_notice(
    monkeypatch,
):
    project, plan = uuid4(), uuid4()
    cb = _callback(monkeypatch, project, info=_info(
        plan, status="active", history=True, purchased=False,
    ))
    bot = _bot()
    await cb(_query("plan:" + plan.hex), bot)
    assert "Неоплаченные счета" in bot.send_message.await_args.args[1]
    assert any(data.startswith("own:pldelete:")
               for _, data in _buttons(bot))
