"""One-tap command in invoice notifications opens an authorized tenant card."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.customer_shortcodes import (
    customer_command,
    parse_customer_command,
)
from telegram_membership_saas.modules.owner_setup.subscribers import SubscriberDetail


@pytest.mark.parametrize("telegram_id", [1, 8353774845, 2**63 - 1])
def test_short_command_roundtrip(telegram_id):
    customer = uuid4()
    code = customer_command(telegram_id=telegram_id, customer_id=customer)
    assert code.startswith("/u") and len(code) <= 15
    assert parse_customer_command(code) == (telegram_id, None)


def test_legacy_customer_id_command_roundtrip():
    customer = uuid4()
    code = customer_command(telegram_id=None, customer_id=customer)
    assert len(code) == 28 and code.startswith("/c")
    assert parse_customer_command(code) == (None, customer)


@pytest.mark.parametrize(
    "code",
    ["/u0", "/u00a", "/uA", "/u-abc", "/u" + "z" * 14,
     "/u1@otherbot", "/c" + "z" * 26, "/cnot-canonical",
     "/start", "u123", "/u" + "z" * 13],
)
def test_invalid_commands_rejected(code):
    with pytest.raises(ValueError):
        parse_customer_command(code)


def _message(command, *, chat_type="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=456, username="owner", first_name="Owner",
        ),
        chat=SimpleNamespace(type=chat_type, id=456 if chat_type == "private" else -23),
        text=command,
    )


def _detail(customer):
    return SubscriberDetail(
        customer_id=customer,
        telegram_id=8353774845,
        username="buyer", first_name="Buyer",
        project_timezone="Europe/Moscow", owner_only_actions=False,
        subscriptions=(), payments=(), total_payments=0, history_page=0,
        grants=(), total_grants=0, grant_page=0, access=(), resources=(),
        total_invoices=3, pending_invoices=1,
    )


def _short_handler(project):
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    return next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "customer_shortcut_handler"
    )


async def test_tap_opens_original_card_without_search_or_extra_buttons(monkeypatch):
    project, actor, customer = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    manager = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "can_manage_project", manager)
    lookup = AsyncMock(return_value=customer)
    monkeypatch.setattr(ui, "customer_id_by_telegram_id", lookup)
    detail = AsyncMock(return_value=_detail(customer))
    monkeypatch.setattr(ui, "subscriber_detail", detail)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    handler = _short_handler(project)
    code = customer_command(telegram_id=8353774845, customer_id=customer)

    await handler(_message(code), bot)
    manager.assert_awaited_once()
    lookup.assert_awaited_once()
    assert lookup.await_args.kwargs["telegram_id"] == 8353774845
    assert lookup.await_args.kwargs["project_id"] == project
    detail.assert_awaited_once()
    assert detail.await_args.kwargs["customer_id"] == customer
    assert "👤" in bot.send_message.await_args.args[1]
    assert any(
        "История счетов" in button.text
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    )


async def test_short_command_rejects_unauthorized_central_and_group(monkeypatch):
    project, actor, customer = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    allowed = AsyncMock(return_value=False)
    monkeypatch.setattr(ui, "can_manage_project", allowed)
    detail = AsyncMock()
    monkeypatch.setattr(ui, "subscriber_detail", detail)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    code = customer_command(telegram_id=8353774845, customer_id=customer)

    await _short_handler(project)(_message(code), bot)
    assert "Нет доступа" in bot.send_message.await_args.args[1]
    detail.assert_not_awaited()

    bot.send_message.reset_mock()
    await _short_handler(project)(_message(code, chat_type="group"), bot)
    bot.send_message.assert_not_awaited()

    bot.send_message.reset_mock()
    await _short_handler(None)(_message(code), bot)
    assert "подключённом боте" in bot.send_message.await_args.args[1]
    detail.assert_not_awaited()


async def test_legacy_shortcut_cannot_bypass_project_lookup(monkeypatch):
    project, actor, customer = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    lookup = AsyncMock()
    monkeypatch.setattr(ui, "customer_id_by_telegram_id", lookup)
    detail = AsyncMock(side_effect=ValueError("Подписчик не найден в этом боте"))
    monkeypatch.setattr(ui, "subscriber_detail", detail)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    code = customer_command(telegram_id=None, customer_id=customer)

    await _short_handler(project)(_message(code), bot)
    lookup.assert_not_awaited()
    assert detail.await_args.kwargs["customer_id"] == customer
    assert "не найден" in bot.send_message.await_args.args[1]
