"""Broken BotFather bot: visible orphan-action resolution with owner confirmation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.bots.deletion_service import RetirementBlockers
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.obligations import OwnerObligations
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.telegram_menus import commands_for
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _query(action):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(
            id=123456, username="owner", first_name="Owner",
        ),
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )


def _bot():
    bot = MagicMock()
    bot.id = 100001
    bot.send_message = AsyncMock()
    return bot


def _handler(monkeypatch, project, *, central=True):
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(
        return_value=project,
    ))
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=None if central else project,
    )
    return next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "owner_callback"
    )


def _overview(project, bot_instance, *, owner=True):
    return SetupOverview(
        project_id=project, project_name="Orphan bot",
        timezone="Europe/Moscow", bots=(("dead_bot", "registered"),),
        resources=(), plans=(), methods=(),
        bot_instances=((bot_instance, "dead_bot", "registered"),),
        can_delete_bot=owner,
    )


def _obligations(*, orphan_only=True):
    return OwnerObligations(
        blockers=RetirementBlockers(
            access_actions=1, requests=0 if orphan_only else 1,
        ),
        payments=(), subscriptions=(), grants=(), access=(),
    )


def _callbacks(call):
    return [
        b.callback_data
        for row in call.kwargs["reply_markup"].inline_keyboard
        for b in row if b.callback_data
    ]


async def test_orphan_owner_sees_button_confirms_and_returns_to_card(monkeypatch):
    project, instance = uuid4(), uuid4()
    ref, bref = encode_uuid(project), encode_uuid(instance)
    cb = _handler(monkeypatch, project)
    monkeypatch.setattr(ui, "list_owner_obligations", AsyncMock(
        return_value=_obligations(),
    ))
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=_overview(project, instance),
    ))
    finish = AsyncMock(return_value=1)
    monkeypatch.setattr(
        ui, "retire_unreachable_bot_with_orphan_actions", finish,
    )
    bot = _bot()
    await cb(_query("oblist"), bot)
    actions = _callbacks(bot.send_message.await_args)
    assert "own:orphanask:" + ref + ":" + bref in actions
    assert all(len(x.encode()) <= 64 for x in actions)

    await cb(_query("orphanask:" + ref + ":" + bref), bot)
    text = bot.send_message.await_args.args[1]
    assert "не" in text and "Telegram" in text
    actions = _callbacks(bot.send_message.await_args)
    assert "own:orphanok:" + ref + ":" + bref in actions
    finish.assert_not_awaited()

    await cb(_query("orphanok:" + ref + ":" + bref), bot)
    finish.assert_awaited_once()
    assert finish.await_args.kwargs["project_id"] == project
    assert finish.await_args.kwargs["bot_instance_id"] == instance
    assert "Отключение запущено" in bot.send_message.await_args_list[-2].args[1]


async def test_other_obligations_do_not_offer_override(monkeypatch):
    project, instance = uuid4(), uuid4()
    cb = _handler(monkeypatch, project)
    monkeypatch.setattr(ui, "list_owner_obligations", AsyncMock(
        return_value=_obligations(orphan_only=False),
    ))
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=_overview(project, instance),
    ))
    bot = _bot()
    await cb(_query("oblist"), bot)
    assert not any(
        x.startswith("own:orphanask:")
        for x in _callbacks(bot.send_message.await_args)
    )


async def test_foreign_project_and_connected_bot_cannot_retire(monkeypatch):
    project, foreign, instance = uuid4(), uuid4(), uuid4()
    ref, bref = encode_uuid(project), encode_uuid(instance)
    cb = _handler(monkeypatch, project)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=_overview(project, instance),
    ))
    monkeypatch.setattr(ui, "list_owner_obligations", AsyncMock(
        return_value=_obligations(),
    ))
    finish = AsyncMock(return_value=1)
    monkeypatch.setattr(
        ui, "retire_unreachable_bot_with_orphan_actions", finish,
    )
    wrong = _query("orphanok:" + encode_uuid(foreign) + ":" + bref)
    await cb(wrong, _bot())
    assert wrong.answer.await_args.kwargs["show_alert"] is True
    finish.assert_not_awaited()

    connected = _handler(monkeypatch, project, central=False)
    guarded = _query("orphanok:" + ref + ":" + bref)
    await connected(guarded, _bot())
    assert guarded.answer.await_args.kwargs["show_alert"] is True
    finish.assert_not_awaited()


async def test_non_owner_never_gets_override_option(monkeypatch):
    project, instance = uuid4(), uuid4()
    cb = _handler(monkeypatch, project)
    monkeypatch.setattr(ui, "list_owner_obligations", AsyncMock(
        return_value=_obligations(),
    ))
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        return_value=_overview(project, instance, owner=False),
    ))
    bot = _bot()
    await cb(_query("oblist"), bot)
    assert not any(
        x.startswith("own:orphanask:")
        for x in _callbacks(bot.send_message.await_args)
    )
