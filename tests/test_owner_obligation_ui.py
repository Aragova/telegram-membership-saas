"""Owner remediation UI: confirm every destructive action and reject stale menus."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.bots.deletion_service import RetirementBlockers
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.obligations import (
    OwnerChange,
    OwnerObligations,
    OwnerPayment,
    OwnerSubscription,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _handler():
    router = ui.create_owner_setup_router(MagicMock())
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )


def _callback(action):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(id=123456, username="owner", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


def _snapshot(payment_id, subscription_id):
    return OwnerObligations(
        blockers=RetirementBlockers(requests=1, subscriptions=1, grants=1, access=2),
        payments=(OwnerPayment(payment_id, 456, "buyer", "waiting_review", "150", "RUB"),),
        subscriptions=(
            OwnerSubscription(subscription_id, 456, "buyer", "VIP", None),
        ),
        grants=(),
        access=(),
    )


async def test_owner_sees_remediation_links_and_warning(monkeypatch):
    actor_id, project_id, payment_id, subscription_id = (
        uuid4(), uuid4(), uuid4(), uuid4()
    )
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project_id))
    monkeypatch.setattr(
        ui, "list_owner_obligations",
        AsyncMock(return_value=_snapshot(payment_id, subscription_id)),
    )
    bot = _bot()
    await _handler()(_callback("oblist"), bot)
    sent = bot.send_message.await_args
    assert "Обязательства" in sent.args[1]
    assert "Заявки: 1" in sent.args[1]
    callbacks = [
        b.callback_data
        for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row if b.callback_data
    ]
    assert "own:ob:pay:" + encode_uuid(project_id) + ":" + encode_uuid(payment_id) in callbacks
    assert "own:ob:sub:" + encode_uuid(project_id) + ":" + encode_uuid(subscription_id) in callbacks
    assert all(len(x.encode()) <= 64 for x in callbacks)

    bot.reset_mock()
    await _handler()(
        _callback("ob:sub:" + encode_uuid(project_id) + ":" + encode_uuid(subscription_id)),
        bot,
    )
    assert "возврат" in bot.send_message.await_args.args[1].lower()
    confirm_buttons = [
        b.callback_data for row in
        bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row if b.callback_data
    ]
    expected = (
        "own:obok:sub:" + encode_uuid(project_id)
        + ":" + encode_uuid(subscription_id)
    )
    assert expected in confirm_buttons


async def test_foreign_project_confirmation_does_not_mutate(monkeypatch):
    actor_id, current, other, payment_id = uuid4(), uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=current))
    close = AsyncMock()
    monkeypatch.setattr(ui, "close_owner_payment", close)
    query = _callback("obok:pay:" + encode_uuid(other) + ":" + encode_uuid(payment_id))
    await _handler()(query, _bot())
    close.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_confirm_payment_uses_tenant_scoped_service(monkeypatch):
    actor, project, payment, subscription = uuid4(), uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(
        ui, "list_owner_obligations",
        AsyncMock(return_value=_snapshot(payment, subscription)),
    )
    close = AsyncMock(return_value=OwnerChange(True, None, "Closed"))
    monkeypatch.setattr(ui, "close_owner_payment", close)
    bot = _bot()
    await _handler()(
        _callback("obok:pay:" + encode_uuid(project) + ":" + encode_uuid(payment)),
        bot,
    )
    close.assert_awaited_once()
    assert len(close.await_args.args) == 1
    assert close.await_args.kwargs == {
        "actor_id": actor, "project_id": project, "request_id": payment,
    }
    assert "Состояние сохранено" in bot.send_message.await_args_list[0].args[1]
    assert "Обязательства" in bot.send_message.await_args_list[-1].args[1]


async def test_stale_payment_confirmation_fails_closed(monkeypatch):
    actor, project, payment = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(
        ui, "list_owner_obligations",
        AsyncMock(return_value=OwnerObligations(
            blockers=RetirementBlockers(), payments=(), subscriptions=(),
            grants=(), access=(),
        )),
    )
    close = AsyncMock()
    monkeypatch.setattr(ui, "close_owner_payment", close)
    query = _callback("obok:pay:" + encode_uuid(project) + ":" + encode_uuid(payment))
    await _handler()(query, _bot())
    close.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True
