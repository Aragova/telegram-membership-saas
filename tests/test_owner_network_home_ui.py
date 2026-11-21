"""Bilingual contract tests for the central ViMi Pay network main menu."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.network_home_ui import (
    owner_network_home_classic,
    owner_network_home_rich,
)
from telegram_membership_saas.modules.owner_setup.service import OwnerHome
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid
from telegram_membership_saas.rich_messages import RICH_ACTIONS_MARKER
from telegram_membership_saas.telegram_menus import commands_for


def snapshot(*, invoice_id=None, sale_rows=None, username="vimitest21bot",
             same_day_count=1):
    future = (
        {
            "invoice_id": str(invoice_id),
            "username": username,
            "due_at": "2026-12-01T10:00:00+03:00",
            "remaining_rub": "150.00",
            "same_day_count": same_day_count,
        }
        if invoice_id else None
    )
    return {
        "snapshot_at": "2026-11-21T13:00:00+03:00",
        "network": {"connected": 29, "healthy": 27,
                    "attention": 1, "down": 1},
        "revenue": sale_rows if sale_rows is not None else [
            {"currency": "RUB", "gross": "127850", "purchases": 55},
            {"currency": "USD", "gross": "425.0", "purchases": 5},
            {"currency": "XTR", "gross": "1260", "purchases": 10},
            {"currency": "TON", "gross": "18.750", "purchases": 3},
        ],
        "billing": {"unpaid": 2, "overdue": 0,
                    "overdue_rub": "0", "next": future},
    }


@pytest.mark.parametrize("lang,title,network,revenue,invoice", [
    ("ru", "ГЛАВНОЕ МЕНЮ", "ВАША СЕТЬ", "ВЫРУЧКА ВСЕЙ СЕТИ",
     "Ближайший платёж"),
    ("en", "MAIN MENU", "YOUR NETWORK", "NETWORK SALES",
     "Next payment"),
])
def test_main_rich_and_fallback_match_real_data(lang, title, network,
                                                revenue, invoice):
    data = snapshot(invoice_id=uuid4())
    token = _language.set(lang)
    try:
        rich = owner_network_home_rich(data)
        classic = owner_network_home_classic(data)
    finally:
        _language.reset(token)
    for screen in (rich, classic):
        for item in (title, network, revenue, invoice,
                     "@vimitest21bot", "01.12.2026", "150 ₽",
                     "29", "27", "1", "127 850 ₽", "425 $",
                     "1 260 ⭐", "18.75 TON"):
            assert item in screen
        assert "0 TON" not in screen
        assert "2026-12-01T" not in screen
        assert "income_usd" not in screen
        assert "private-invoice" not in screen
    assert RICH_ACTIONS_MARKER in rich
    assert "<table bordered compact>" in rich
    assert "<b>" in classic and "<i>" in classic


def test_no_zero_currencies_and_no_fabricated_nearest_due():
    data = snapshot(sale_rows=[
        {"currency": "RUB", "gross": "0", "purchases": 0},
        {"currency": "BYN", "gross": "2.50", "purchases": 1},
    ])
    rich = owner_network_home_rich(data)
    classic = owner_network_home_classic(data)
    for output in (rich, classic):
        assert "2.5 Br" in output
        assert "RUB" not in output
        assert "Next payment" not in output
        assert "Ближайший платёж" not in output
        assert "0 ₽" not in output  # no phantom debt or blank currency row
        assert "📋" in output and "2" in output


def test_compact_same_day_invoices_and_disconnected_bot_fallback():
    data = snapshot(invoice_id=uuid4(), username=None, same_day_count=3)
    text = owner_network_home_classic(data)
    assert "Ранее отключённый бот" in text
    assert "+ ещё 2" in text
    assert "01.12.2026" in text
    assert "Счёт аккаунта" in text


def test_username_escaped_and_no_uuids_leaked():
    private_invoice = uuid4()
    data = snapshot(invoice_id=private_invoice, username="bad<&bot")
    rich = owner_network_home_rich(data)
    assert "@bad&lt;&amp;bot" in rich
    assert "@bad<&bot" not in rich
    assert str(private_invoice) not in rich


def test_native_commands_are_exactly_three_for_central_and_platform():
    for role in ("central", "platform"):
        for lang in ("ru", "en"):
            commands = commands_for(role, lang)
            assert [x.command for x in commands] == ["start", "bots", "billing"]
            if lang == "ru":
                assert "Счёт аккаунта" in commands[2].description
            else:
                assert "Account bill" in commands[2].description
    assert "billing" not in [x.command for x in commands_for("owner", "ru")]


def _message(command="/start"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        chat=SimpleNamespace(id=123, type="private"),
        text=command,
    )


def _bot():
    result = MagicMock()
    result.id = 555666
    result.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=321),
    )
    result.set_my_commands = AsyncMock()
    return result


def _home_handler():
    router = ui.create_owner_setup_router(MagicMock())
    return next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "home_message"
    )


@pytest.mark.parametrize("command", ["/start", "/menu"])
async def test_start_or_menu_is_fleet_dashboard_not_last_bot(monkeypatch, command):
    owner, selected, invoice = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "owner_home", AsyncMock(
        return_value=OwnerHome(((selected, "Another bot"),), selected),
    ))
    spy = AsyncMock(return_value=snapshot(invoice_id=invoice))
    monkeypatch.setattr(ui, "read_owner_network_home", spy)
    setup_overview = AsyncMock(side_effect=AssertionError(
        "Must not open last selected bot when /start is pressed",
    ))
    monkeypatch.setattr(ui, "setup_overview", setup_overview)
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    instance = _bot()
    await _home_handler()(_message(command), instance)
    args = instance.send_message.await_args
    assert "ГЛАВНОЕ МЕНЮ" in args.args[1]
    assert "Another bot" not in args.args[1]
    assert str(invoice) not in args.args[1]
    buttons = args.kwargs["reply_markup"].inline_keyboard
    assert [len(row) for row in buttons] == [2, 2, 1, 1]
    assert [b.style for b in buttons[0]] == ["success", "success"]
    assert [b.style for b in buttons[1]] == ["primary", "primary"]
    assert buttons[2][0].callback_data == (
        "own:saasbill:" + encode_uuid(invoice) + ":0"
    )
    assert buttons[2][0].style == "primary"
    assert buttons[3][0].url == "https://t.me/vimi_support_bot"
    assert buttons[3][0].style == "success"
    assert [b.callback_data for b in buttons[0]] == [
        "own:projects", "own:substart",
    ]
    assert buttons[1][0].callback_data == "own:saasinvoices:0"
    assert buttons[1][1].callback_data == "lng:menu"
    spy.assert_awaited_once_with(ui.factory if hasattr(ui, "factory") else
                                 spy.await_args.args[0], actor_id=owner)
    setup_overview.assert_not_awaited()


async def test_bots_command_always_opens_registry(monkeypatch):
    owner, selected = uuid4(), uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "owner_home", AsyncMock(return_value=OwnerHome(
        ((selected, "Selected bot"),), selected,
    )))
    monkeypatch.setattr(ui, "list_archived_owner_connections", AsyncMock(
        return_value=(),
    ))
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(
        side_effect=AssertionError("Do not open selected bot"),
    ))
    instance = _bot()
    await _home_handler()(_message("/bots"), instance)
    assert "Мои подключения" in instance.send_message.await_args.args[1]
    assert "own:select:" + selected.hex in [
        b.callback_data
        for row in instance.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard for b in row
    ]


async def test_summary_unavailable_never_invents_zeros(monkeypatch):
    owner = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=owner))
    monkeypatch.setattr(ui, "owner_home", AsyncMock(
        return_value=OwnerHome(((uuid4(), "bot"),), None),
    ))
    monkeypatch.setattr(ui, "read_owner_network_home", AsyncMock(
        side_effect=RuntimeError("db temporarily offline"),
    ))
    monkeypatch.setattr(ui, "cancel_owner_flow", AsyncMock())
    instance = _bot()
    await _home_handler()(_message(), instance)
    message = instance.send_message.await_args.args[1]
    assert "временно недоступна" in message
    assert "0 ₽" not in message
    rows = instance.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    assert rows[0][0].callback_data == "own:projects"
    assert rows[1][0].callback_data == "own:saasinvoices:0"
