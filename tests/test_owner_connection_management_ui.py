"""Central registrar gives every project a safe, compact management card."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.connection_management import (
    ConnectionDisposition,
)
from telegram_membership_saas.modules.owner_setup.service import (
    OwnerHome,
    SetupFlow,
    SetupOverview,
)


def bot():
    result = MagicMock()
    result.id = 1001001
    result.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=123),
    )
    return result


def query(action):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            message_id=100,
        ),
        from_user=SimpleNamespace(
            id=123, first_name="Owner", username="owner",
        ),
        answer=AsyncMock(),
    )


def handler(monkeypatch, *, customer_project=None, selected=None):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(
        return_value=selected,
    ))
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=customer_project,
    )
    return next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "owner_callback"
    )


def overview(project_id, *, has_bot=False, owner=True):
    return SetupOverview(
        project_id=project_id,
        project_name="ViMi Pay E2E",
        timezone="Europe/Moscow",
        bots=(("test_bot", "registered"),) if has_bot else (),
        resources=(),
        plans=(),
        methods=(),
        bot_instances=((uuid4(), "test_bot", "registered"),) if has_bot else (),
        can_delete_bot=owner,
    )


def buttons(reply):
    return [
        x.callback_data for row in reply.kwargs["reply_markup"].inline_keyboard
        for x in row if x.callback_data
    ]


async def test_connection_list_paginates_100_without_overflow(monkeypatch):
    ids = [(uuid4(), f"Brand {i}") for i in range(100)]
    monkeypatch.setattr(ui, "owner_home", AsyncMock(
        return_value=OwnerHome(tuple(ids), None),
    ))
    monkeypatch.setattr(ui, "list_archived_owner_connections",
                        AsyncMock(return_value=()))
    cb = handler(monkeypatch)
    instance = bot()
    await cb(query("projects"), instance)
    first = buttons(instance.send_message.await_args)
    assert len([x for x in first if x.startswith("own:select:")]) == 10
    assert "own:projects:1" in first
    await cb(query("projects:9"), instance)
    last = buttons(instance.send_message.await_args)
    assert len([x for x in last if x.startswith("own:select:")]) == 10
    assert "own:projects:8" in last
    assert "own:projects:10" not in last
    assert all(len(x.encode()) <= 64 for x in last)


async def test_select_unused_connection_shows_rename_and_delete(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "choose_project", AsyncMock())
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id),
    ))
    monkeypatch.setattr(ui, "get_connection_disposition", AsyncMock(
        return_value=ConnectionDisposition(False, False),
    ))
    instance = bot()
    await cb(query("select:" + project_id.hex), instance)
    actions = buttons(instance.send_message.await_args)
    assert "own:connrename:" + project_id.hex in actions
    assert "own:connremove:" + project_id.hex in actions
    assert "own:projects" in actions
    assert "own:drop:" not in str(actions)
    ui.choose_project.assert_awaited_once()


async def test_connection_with_history_shows_archive_not_delete(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id),
    ))
    monkeypatch.setattr(ui, "get_connection_disposition", AsyncMock(
        return_value=ConnectionDisposition(False, True),
    ))
    instance = bot()
    await cb(query("bots"), instance)
    actions = buttons(instance.send_message.await_args)
    assert "own:connarchive:" + project_id.hex in actions
    assert "own:connremove:" + project_id.hex not in actions


async def test_connected_bot_has_safe_retirement_not_project_delete(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id, has_bot=True),
    ))
    instance = bot()
    await cb(query("bots"), instance)
    actions = buttons(instance.send_message.await_args)
    assert any(x.startswith("own:drop:") for x in actions)
    assert not any(x.startswith("own:connremove:") for x in actions)


async def test_confirm_deletion_checks_selected_project_and_owner(monkeypatch):
    project_id, other_id = uuid4(), uuid4()
    cb = handler(monkeypatch, selected=other_id)
    monkeypatch.setattr(ui, "remove_unused_connection", AsyncMock())
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id),
    ))
    instance = bot()
    req = query("connremoveok:" + project_id.hex)
    await cb(req, instance)
    ui.remove_unused_connection.assert_not_awaited()
    req.answer.assert_awaited_once()
    assert req.answer.await_args.kwargs.get("show_alert")

    # Even when the selected project matches, a nonowner cannot delete.
    ui.get_selected_project.return_value = project_id
    ui.setup_overview.return_value = overview(project_id, owner=False)
    req = query("connremoveok:" + project_id.hex)
    await cb(req, instance)
    ui.remove_unused_connection.assert_not_awaited()
    assert req.answer.await_args.kwargs.get("show_alert")


async def test_owner_deletion_is_two_explicit_steps(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=overview(project_id),
    ))
    monkeypatch.setattr(ui, "get_connection_disposition", AsyncMock(
        return_value=ConnectionDisposition(False, False),
    ))
    monkeypatch.setattr(ui, "remove_unused_connection", AsyncMock())
    monkeypatch.setattr(ui, "owner_home", AsyncMock(
        return_value=OwnerHome((), None),
    ))
    monkeypatch.setattr(ui, "list_archived_owner_connections",
                        AsyncMock(return_value=()))
    instance = bot()
    await cb(query("connremove:" + project_id.hex), instance)
    assert "own:connremoveok:" + project_id.hex in buttons(
        instance.send_message.await_args,
    )
    ui.remove_unused_connection.assert_not_awaited()
    await cb(query("connremoveok:" + project_id.hex), instance)
    ui.remove_unused_connection.assert_awaited_once()
    assert "own:projects" not in buttons(instance.send_message.await_args)
    assert "own:new" in buttons(instance.send_message.await_args)


async def test_rename_connection_central_only(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, selected=project_id)
    monkeypatch.setattr(ui, "get_connection_disposition", AsyncMock(
        return_value=ConnectionDisposition(False, False),
    ))
    monkeypatch.setattr(ui, "set_flow", AsyncMock(return_value="generation"))
    monkeypatch.setattr(ui, "bind_prompt", AsyncMock())
    instance = bot()
    await cb(query("connrename:" + project_id.hex), instance)
    assert "название подключения" in instance.send_message.await_args.args[1]
    assert instance.send_message.await_args.kwargs["reply_markup"].selective
    ui.set_flow.assert_awaited_once()

    # A saved prompt from another project must never rename the selected one.
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=SetupFlow(
        "connection_rename", uuid4(), {}, 123, "generation",
    )))
    monkeypatch.setattr(ui, "rename_owner_connection", AsyncMock())
    reply = SimpleNamespace(
        from_user=SimpleNamespace(id=123, first_name="Owner", username="owner"),
        chat=SimpleNamespace(id=123, type="private"),
        text="New label",
        message_id=200,
        reply_to_message=SimpleNamespace(
            message_id=123,
            from_user=SimpleNamespace(id=instance.id),
        ),
    )
    router = ui.create_owner_setup_router(MagicMock())
    reply_handler = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "owner_reply_handler"
    )
    await reply_handler(reply, instance)
    ui.rename_owner_connection.assert_not_awaited()


async def test_last_archived_connection_is_reachable_from_central_start(monkeypatch):
    archived_id = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(ui, "owner_home", AsyncMock(
        return_value=OwnerHome((), None, archived_count=1),
    ))
    monkeypatch.setattr(ui, "list_archived_owner_connections", AsyncMock(
        return_value=((archived_id, "Archived E2E"),),
    ))
    monkeypatch.setattr(ui, "set_chat_commands", AsyncMock())
    router = ui.create_owner_setup_router(MagicMock())
    home_message = next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "home_message"
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=123, first_name="Owner", username="owner",
        ),
        chat=SimpleNamespace(id=123, type="private"),
        text="/menu",
    )
    instance = bot()
    await home_message(message, instance)
    assert "own:archives" in buttons(instance.send_message.await_args)
    assert "own:new" in buttons(instance.send_message.await_args)
