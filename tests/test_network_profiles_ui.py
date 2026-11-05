"""Connected owner bot network profiles are explicit opt-in, not central actions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.network_profiles import (
    NetworkProfileInfo,
)


def bot():
    instance = MagicMock()
    instance.id = 900
    instance.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=22),
    )
    return instance


def query(action):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=SimpleNamespace(id=123456, first_name="Owner", username="owner"),
        answer=AsyncMock(),
    )


def actions(instance):
    return [
        btn.callback_data
        for row in instance.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]


def handler(monkeypatch, project_id=None):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project_id))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project_id,
    )
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )


async def test_network_setup_offers_explicit_bank_details_consent(monkeypatch):
    cb = handler(monkeypatch, uuid4())
    instance = bot()
    await cb(query("networkask"), instance)
    buttons = actions(instance)
    assert "own:networksave:yes" in buttons
    assert "own:networksave:no" in buttons
    assert "own:network" in buttons
    assert all(len(data.encode("utf-8")) <= 64 for data in buttons)


async def test_network_save_is_owner_only_and_no_new_native_menu(monkeypatch):
    project_id = uuid4()
    cb = handler(monkeypatch, project_id)
    instance = bot()
    saved = NetworkProfileInfo(
        enabled=True, version=1, plan_count=2,
        method_count=1, source="Original", copy_instructions=True,
    )
    save = AsyncMock(return_value=saved)
    monkeypatch.setattr(ui, "save_owner_network_profile", save)
    monkeypatch.setattr(ui, "read_owner_network_profile",
                        AsyncMock(return_value=saved))
    await cb(query("networksave:yes"), instance)
    assert save.await_args.kwargs["source_project_id"] == project_id
    assert save.await_args.kwargs["copy_instructions"] is True
    assert "own:networkoff" in actions(instance)
    assert "own:dashboard" in actions(instance)
    assert "✅" in instance.send_message.await_args_list[-2].args[1]

    monkeypatch.setattr(ui, "set_network_auto_apply", AsyncMock())
    await cb(query("networkoff"), instance)
    ui.set_network_auto_apply.assert_awaited_once()

    # The central bot must not accept tenant profile administration.
    central = handler(monkeypatch, None)
    save.reset_mock()
    other = bot()
    await central(query("networksave:yes"), other)
    save.assert_not_awaited()
    other.send_message.assert_not_awaited()
    assert query("networksave:yes").data.startswith("own:")
