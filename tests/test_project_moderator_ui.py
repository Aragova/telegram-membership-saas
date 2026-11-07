"""Support UI is isolated from the existing owner and financial callbacks."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from test_owner_subscribers_ui import _bot, _buttons, _callbacks, _query

from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.owner_setup import moderator_handlers as mod_ui
from telegram_membership_saas.modules.owner_setup.moderators import (
    ModeratorAccount,
    ModeratorCustomer,
    ModeratorReview,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _buyer():
    return ModeratorCustomer(
        id=uuid4(), telegram_id=12345678, username="testbuyer",
        first_name="Buyer", is_banned=False, invoice_count=2,
    )


def _query_mod(action):
    return SimpleNamespace(
        data="mod:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(
            type="private", id=123,
        )),
        from_user=SimpleNamespace(
            id=123, username="support", first_name="Staff",
        ),
        answer=AsyncMock(),
    )


def _router(project):
    return mod_ui.create_moderator_router(MagicMock(), project_id=project)


def _callback(router):
    return next(h.callback for h in router.callback_query.handlers
                if h.callback.__name__ == "moderator_callback")


async def test_staff_board_is_read_only_for_payments_and_can_open_buyer(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(mod_ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(mod_ui, "can_use_moderator_panel", AsyncMock(
        return_value=True,
    ))
    monkeypatch.setattr(mod_ui, "list_moderator_customers", AsyncMock(
        return_value=(1, (buyer,)),
    ))
    monkeypatch.setattr(mod_ui, "find_moderator_customer", AsyncMock(
        return_value=buyer,
    ))
    monkeypatch.setattr(mod_ui, "list_moderator_reviews", AsyncMock(
        return_value=(ModeratorReview(
            id=uuid4(), telegram_id=12345678, username="testbuyer",
            plan_name="Premium", submitted_at=None,
        ),),
    ))
    router = _router(project)
    cb = _callback(router)
    bot = _bot()
    await cb(_query_mod("buyers:0"), bot)
    assert "mod:buyer:" + encode_uuid(buyer.id) in _buttons(bot)
    await cb(_query_mod("buyer:" + encode_uuid(buyer.id)), bot)
    actions = _buttons(bot)
    assert "mod:compose:" + encode_uuid(buyer.id) in actions
    assert "mod:banask:" + encode_uuid(buyer.id) + ":1" in actions
    assert not any(x.startswith(("own:", "pay:", "bc:")) for x in actions)
    await cb(_query_mod("reviews"), bot)
    text = bot.send_message.await_args.args[1]
    assert "заяв" in text.lower() or "review" in text.lower()
    assert "500 RUB" not in text and "receipt" not in text.lower()
    assert not any("approve" in x or "reject" in x for x in _buttons(bot))


async def test_moderator_ban_is_confirmed_and_rechecked(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(mod_ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(mod_ui, "can_use_moderator_panel", allowed)
    monkeypatch.setattr(mod_ui, "find_moderator_customer", AsyncMock(
        return_value=buyer,
    ))
    ban = AsyncMock(return_value=True)
    monkeypatch.setattr(mod_ui, "set_moderated_customer_ban", ban)
    router = _router(project)
    cb = _callback(router)
    bot = _bot()
    cid = encode_uuid(buyer.id)
    await cb(_query_mod("banask:" + cid + ":1"), bot)
    ban.assert_not_awaited()
    assert "mod:banok:" + cid + ":1" in _buttons(bot)
    await cb(_query_mod("banok:" + cid + ":1"), bot)
    ban.assert_awaited_once()
    assert ban.await_args.kwargs["banned"]
    assert ban.await_args.kwargs["project_id"] == project
    allowed.return_value = False
    req = _query_mod("banok:" + cid + ":0")
    await cb(req, bot)
    assert req.answer.await_args.kwargs["show_alert"]
    assert ban.await_count == 1


async def test_moderator_compose_uses_one_message_and_drops_on_revocation(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(mod_ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    allowed = AsyncMock(return_value=True)
    monkeypatch.setattr(mod_ui, "can_use_moderator_panel", allowed)
    monkeypatch.setattr(mod_ui, "find_moderator_customer", AsyncMock(
        return_value=buyer,
    ))
    router = _router(project)
    cb = _callback(router)
    composing = next(h for h in router.message.handlers
                     if h.callback.__name__ == "moderator_compose")
    filt = composing.filters[0].callback
    bot = _bot()
    message = SimpleNamespace(
        text="Здравствуйте 👋",
        from_user=SimpleNamespace(id=123, username="staff", first_name="Staff"),
        chat=SimpleNamespace(type="private", id=123),
        reply_to_message=None,
    )
    assert not await filt(message)
    await cb(_query_mod("compose:" + encode_uuid(buyer.id)), bot)
    assert await filt(message)
    # Simulate the saved buyer language without database access.
    from contextlib import asynccontextmanager

    from telegram_membership_saas.localization import recipient_language
    @asynccontextmanager
    async def locale(factory, telegram_id):
        async with recipient_language(None, telegram_id):
            yield
    monkeypatch.setattr(mod_ui, "recipient_language", locale)
    await composing.callback(message, bot)
    assert not await filt(message)
    assert any(
        c.args[0] == buyer.telegram_id and "Здравствуйте" in c.args[1]
        for c in bot.send_message.await_args_list
    )
    # Fresh composer is invalidated immediately when support role revoked.
    await cb(_query_mod("compose:" + encode_uuid(buyer.id)), bot)
    allowed.return_value = False
    before = len(bot.send_message.await_args_list)
    await composing.callback(message, bot)
    assert all(c.args[0] != buyer.telegram_id
               for c in bot.send_message.await_args_list[before:])


async def test_owner_moderators_settings_and_remove_confirmation(monkeypatch):
    project, target = uuid4(), 54321234
    cb, owner = _callbacks(monkeypatch, project)
    list_mods = AsyncMock(return_value=(
        1, (ModeratorAccount(
            telegram_id=target, username="staff", first_name="Mod",
        ),),
    ))
    monkeypatch.setattr(owner_ui, "list_project_moderators", list_mods)
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(owner_ui, "set_project_moderator", remove)
    bot = _bot()
    await cb(_query("modmanage"), bot)
    assert "own:modremoveask:" + str(target) in _buttons(bot)
    assert "own:modadd" in _buttons(bot)
    await cb(_query("modremoveask:" + str(target)), bot)
    remove.assert_not_awaited()
    assert "own:modremoveok:" + str(target) in _buttons(bot)
    await cb(_query("modremoveok:" + str(target)), bot)
    remove.assert_awaited_once()
    assert remove.await_args.kwargs["actor_id"] == owner
    assert remove.await_args.kwargs["enabled"] is False


async def test_mod_command_abandons_old_reply_target(monkeypatch):
    project, buyer = uuid4(), _buyer()
    monkeypatch.setattr(mod_ui, "ensure_telegram_actor", AsyncMock(
        return_value=uuid4(),
    ))
    monkeypatch.setattr(mod_ui, "can_use_moderator_panel", AsyncMock(
        return_value=True,
    ))
    monkeypatch.setattr(mod_ui, "find_moderator_customer", AsyncMock(
        return_value=buyer,
    ))
    router = _router(project)
    cb = _callback(router)
    bot = _bot()
    composing = next(h for h in router.message.handlers
                     if h.callback.__name__ == "moderator_compose")
    filt = composing.filters[0].callback
    message = SimpleNamespace(
        text="Отправить это не надо",
        chat=SimpleNamespace(type="private", id=123),
        from_user=SimpleNamespace(
            id=123, username="mod", first_name="Moderator",
        ),
        reply_to_message=None,
    )
    await cb(_query_mod("compose:" + encode_uuid(buyer.id)), bot)
    assert await filt(message)
    command = next(h.callback for h in router.message.handlers
                   if h.callback.__name__ == "moderator_command")
    message.text = "/mod"
    await command(message, bot)
    message.text = "Отправить это не надо"
    assert not await filt(message)


async def test_customer_card_allows_owner_only_moderator_change(monkeypatch):
    project, customer, resource = uuid4(), uuid4(), uuid4()
    from test_owner_subscribers_ui import _detail

    cb, owner = _callbacks(monkeypatch, project)
    card = _detail(customer, resource)
    monkeypatch.setattr(owner_ui, "subscriber_detail", AsyncMock(
        return_value=card,
    ))
    toggle = AsyncMock(return_value=True)
    monkeypatch.setattr(owner_ui, "set_project_moderator", toggle)
    bot = _bot()
    cid = encode_uuid(customer)
    await cb(_query("subscriber:" + cid), bot)
    assert "own:submod:" + cid in _buttons(bot)
    await cb(_query("submod:" + cid), bot)
    assert "own:submodconfirm:" + cid + ":1" in _buttons(bot)
    assert "own:submodconfirm:" + cid + ":0" in _buttons(bot)
    toggle.assert_not_awaited()
    await cb(_query("submodconfirm:" + cid + ":1"), bot)
    toggle.assert_awaited_once()
    assert toggle.await_args.kwargs["actor_id"] == owner
    assert toggle.await_args.kwargs["telegram_id"] == card.telegram_id
    assert toggle.await_args.kwargs["enabled"] is True

    from dataclasses import replace

    owner_ui.subscriber_detail.return_value = replace(
        card, owner_only_actions=False,
    )
    req = _query("submodconfirm:" + cid + ":0")
    await cb(req, bot)
    assert req.answer.await_args.kwargs["show_alert"]
    assert toggle.await_count == 1
