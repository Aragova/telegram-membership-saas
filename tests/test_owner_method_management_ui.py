"""Payment methods are managed on their own card with safe confirmations."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.tenant_admin import MethodInfo
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _bot():
    bot = MagicMock()
    bot.id = 123
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=101))
    return bot


def _query(action):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private"), message_id=5),
        from_user=SimpleNamespace(id=999, first_name="Owner", username="owner"),
        answer=AsyncMock(),
    )


def _buttons(bot):
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _setup(monkeypatch, project, info):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(ui, "get_owner_method_info", AsyncMock(return_value=info))
    monkeypatch.setattr(ui, "owner_available_currencies",
                        AsyncMock(return_value=("RUB", "USD", "EUR")))
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    return next(x.callback for x in router.callback_query.handlers
                if x.callback.__name__ == "owner_callback")


@pytest.mark.parametrize("history", [True, False])
async def test_method_card_has_correct_owner_controls(monkeypatch, history):
    project, method_id = uuid4(), uuid4()
    info = MethodInfo(
        method_id, "<Bank>", "active", ("RUB", "USD"),
        revision="abc123456789", has_history=history,
        instruction="Send {amount} {currency}",
    )
    cb = _setup(monkeypatch, project, info)
    bot = _bot()
    token = _language.set("en")
    try:
        await cb(_query("method:" + method_id.hex), bot)
        rendered = bot.send_message.await_args.args[1]
        buttons = _buttons(bot)
    finally:
        _language.reset(token)
    assert "&lt;Bank&gt;" in rendered
    assert "Send {amount} {currency}" in rendered
    for action in ("methodrename:", "methodedit:", "methodlink:", "methodconfirmoff:"):
        assert any(b.startswith("own:" + action) for b in buttons)
    assert any(b.startswith("own:methoddeleteask:") for b in buttons) is not history
    assert all(len(b.encode("utf-8")) <= 64 for b in buttons)


async def test_delete_confirmation_is_revision_bound(monkeypatch):
    project, method_id = uuid4(), uuid4()
    info = MethodInfo(method_id, "Unused", "active", ("RUB",),
                      revision="abc123456789", has_history=False)
    cb = _setup(monkeypatch, project, info)
    bot = _bot()
    await cb(_query("methoddeleteask:" + method_id.hex), bot)
    button = next(b for b in _buttons(bot) if b.startswith("own:methoddelete:"))
    assert button == (
        "own:methoddelete:" + encode_uuid(method_id) + ":abc123456789"
    )
    assert len(button.encode()) <= 64


async def test_disable_requires_revision_and_rejects_legacy_callback(monkeypatch):
    project, method_id = uuid4(), uuid4()
    info = MethodInfo(method_id, "Bank", "active", ("RUB",),
                      revision="abc123456789")
    cb = _setup(monkeypatch, project, info)
    bot = _bot()
    monkeypatch.setattr(ui, "change_owner_method_status_guarded", AsyncMock())
    await cb(_query("methodconfirmoff:" + method_id.hex), bot)
    button = next(b for b in _buttons(bot) if b.startswith("own:methodoff:"))
    assert button.endswith(":abc123456789")
    assert len(button.encode()) <= 64
    await cb(_query("methodoff:" + method_id.hex), bot)
    ui.change_owner_method_status_guarded.assert_not_awaited()
