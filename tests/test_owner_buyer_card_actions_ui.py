"""Buyer card actions: safe dialogs, role checks and no fake payment events."""
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from test_owner_subscribers_ui import _bot, _buttons, _callbacks, _detail, _query

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.subscribers import (
    ActiveSubscriptionPage,
    ManualPlanItem,
    SubscriberGrant,
    SubscriberSubscription,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


async def test_ban_needs_confirmation_and_is_owner_only(monkeypatch):
    project, customer, resource = uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    card = _detail(customer, resource)
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(return_value=card))
    update = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "set_customer_ban", update)
    cid = encode_uuid(customer)
    bot = _bot()

    await cb(_query("subscriber:" + cid), bot)
    assert "own:subbanask:" + cid + ":1" in _buttons(bot)
    await cb(_query("subbanask:" + cid + ":1"), bot)
    assert update.await_count == 0
    assert "own:subbanok:" + cid + ":1" in _buttons(bot)
    await cb(_query("subbanok:" + cid + ":1"), bot)
    update.assert_awaited_once()
    assert update.await_args.kwargs["project_id"] == project
    assert update.await_args.kwargs["customer_id"] == customer
    assert update.await_args.kwargs["banned"] is True

    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(
        return_value=replace(card, owner_only_actions=False),
    ))
    req = _query("subbanok:" + cid + ":0")
    await cb(req, bot)
    assert req.answer.await_args.kwargs["show_alert"] is True
    assert update.await_count == 1


async def test_manual_activation_picker_and_confirm_keep_paid_ledger_untouched(monkeypatch):
    project, customer, resource, plan = uuid4(), uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(
        return_value=_detail(customer, resource),
    ))
    monkeypatch.setattr(ui, "list_activatable_plans", AsyncMock(
        return_value=(ManualPlanItem(
            id=plan, name="Premium", duration_value=1,
            duration_unit="month", resource_count=1,
        ),),
    ))
    activate = AsyncMock(return_value=SimpleNamespace(changed=True))
    monkeypatch.setattr(ui, "activate_manual_plan", activate)
    bot = _bot()
    cid, pid = encode_uuid(customer), encode_uuid(plan)

    await cb(_query("subactivate:" + cid), bot)
    assert "own:subactask:" + cid + ":" + pid in _buttons(bot)
    assert activate.await_count == 0
    await cb(_query("subactok:" + cid + ":" + pid), bot)
    activate.assert_awaited_once()
    assert activate.await_args.kwargs["plan_id"] == plan
    assert activate.await_args.kwargs["customer_id"] == customer


async def test_cancel_paid_subscription_has_explicit_confirmation(monkeypatch):
    project, customer, resource, sid = uuid4(), uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    card = replace(_detail(customer, resource), subscriptions=(
        SubscriberSubscription(
            id=sid, plan_name="Premium", status="active",
            period_end=datetime.now(UTC),
        ),
    ))
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(return_value=card))
    monkeypatch.setattr(ui, "list_active_customer_subscriptions", AsyncMock(
        return_value=ActiveSubscriptionPage(
            total=1, page=0, subscriptions=card.subscriptions,
        ),
    ))
    monkeypatch.setattr(ui, "get_active_customer_subscription", AsyncMock(
        return_value=card.subscriptions[0],
    ))
    end = AsyncMock(return_value=SimpleNamespace(
        changed=True, telegram_id=None, message="",
    ))
    monkeypatch.setattr(ui, "end_owner_subscription", end)
    bot = _bot()
    cid, subid = encode_uuid(customer), encode_uuid(sid)
    await cb(_query("subcancel:" + cid), bot)
    assert "own:subendask:" + cid + ":" + subid in _buttons(bot)
    await cb(_query("subendask:" + cid + ":" + subid), bot)
    assert end.await_count == 0
    assert "own:subendok:" + cid + ":" + subid in _buttons(bot)
    await cb(_query("subendok:" + cid + ":" + subid), bot)
    end.assert_awaited_once()
    assert end.await_args.kwargs["subscription_id"] == sid


async def test_owner_message_targets_one_verified_customer(monkeypatch):
    project, customer, resource = uuid4(), uuid4(), uuid4()
    cb, owner = _callbacks(monkeypatch, project)
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(
        return_value=_detail(customer, resource),
    ))
    monkeypatch.setattr(ui, "set_flow", AsyncMock(return_value="generation"))
    monkeypatch.setattr(ui, "bind_prompt", AsyncMock())
    bot = _bot()
    cid = encode_uuid(customer)
    await cb(_query("submsg:" + cid), bot)
    assert ui.set_flow.await_args.kwargs["step"] == "subscriber_message"
    assert ui.set_flow.await_args.kwargs["payload"] == {"customer_id": customer.hex}
    assert ui.set_flow.await_args.kwargs["project_id"] == project

    flow = SimpleNamespace(
        step="subscriber_message", project_id=project,
        payload={"customer_id": customer.hex},
    )
    monkeypatch.setattr(ui, "consume_flow", AsyncMock(return_value=flow))
    router = ui.create_owner_setup_router(None, fixed_project_id=project)
    handler = next(x.callback for x in router.message.handlers
                   if x.callback.__name__ == "owner_reply_handler")
    bot.send_message.reset_mock()
    message = SimpleNamespace(
        from_user=SimpleNamespace(
            id=123, username="owner", first_name="Owner",
        ),
        chat=SimpleNamespace(type="private", id=123),
        reply_to_message=None, text="Привет 👋 https://t.me/example",
    )
    await handler(message, bot)
    assert any(
        call.args == (12345678, "Привет 👋 https://t.me/example")
        for call in bot.send_message.await_args_list
    )


async def test_older_manual_activation_can_be_revoked_from_page_four(monkeypatch):
    project, customer, resource, plan, activation = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    cb, _ = _callbacks(monkeypatch, project)
    detail = replace(
        _detail(customer, resource),
        grants=(SubscriberGrant(
            id=uuid4(), resource_id=resource, resource_name="Channel",
            source="manual", valid_until=None, source_id=activation,
            manual_plan_id=plan, manual_plan_name="Premium",
        ),),
        total_grants=33, grant_page=4,
    )
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(return_value=detail))
    monkeypatch.setattr(ui, "list_active_customer_subscriptions", AsyncMock(
        return_value=ActiveSubscriptionPage(
            total=0, page=0, subscriptions=(),
        ),
    ))
    revoke = AsyncMock(return_value=SimpleNamespace(changed=True))
    monkeypatch.setattr(ui, "revoke_manual_plan", revoke)
    bot = _bot()
    cid, aid = encode_uuid(customer), encode_uuid(activation)
    await cb(_query("subcancel:" + cid + ":4"), bot)
    actions = _buttons(bot)
    ask = "own:sra:" + cid + ":" + aid + ":4"
    confirm = "own:srk:" + cid + ":" + aid + ":4"
    assert ask in actions
    assert "own:subcancel:" + cid + ":3" in actions
    assert all(len(action.encode()) <= 64 for action in actions)
    await cb(_query(ask.removeprefix("own:")), bot)
    assert confirm in _buttons(bot)
    assert revoke.await_count == 0
    await cb(_query(confirm.removeprefix("own:")), bot)
    revoke.assert_awaited_once()
    assert revoke.await_args.kwargs["activation_id"] == activation
    assert ui.subscriber_detail.await_args.kwargs["grant_page"] == 0


async def test_old_active_subscription_paged_and_confirmed_by_id(monkeypatch):
    project, customer, resource, sid = uuid4(), uuid4(), uuid4(), uuid4()
    cb, _ = _callbacks(monkeypatch, project)
    # The short card does not list the old subscription at all.
    monkeypatch.setattr(ui, "subscriber_detail", AsyncMock(
        return_value=_detail(customer, resource),
    ))
    old_paid = SubscriberSubscription(
        id=sid, plan_name="Legacy lifetime", status="active", period_end=None,
    )
    pages = AsyncMock(side_effect=lambda **kwargs: ActiveSubscriptionPage(
        total=17, page=kwargs["page"],
        subscriptions=(old_paid,) if kwargs["page"] == 2 else (),
    ))
    monkeypatch.setattr(ui, "list_active_customer_subscriptions", pages)
    check = AsyncMock(return_value=old_paid)
    monkeypatch.setattr(ui, "get_active_customer_subscription", check)
    end = AsyncMock(return_value=SimpleNamespace(
        changed=True, telegram_id=None, message="",
    ))
    monkeypatch.setattr(ui, "end_owner_subscription", end)
    cid, sub = encode_uuid(customer), encode_uuid(sid)
    bot = _bot()
    await cb(_query("subcancel:" + cid + ":2:0"), bot)
    assert "own:subendask:" + cid + ":" + sub in _buttons(bot)
    assert "own:subcancel:" + cid + ":1:0" in _buttons(bot)
    await cb(_query("subendask:" + cid + ":" + sub), bot)
    check.assert_awaited_once()
    await cb(_query("subendok:" + cid + ":" + sub), bot)
    end.assert_awaited_once()
    assert end.await_args.kwargs["subscription_id"] == sid
