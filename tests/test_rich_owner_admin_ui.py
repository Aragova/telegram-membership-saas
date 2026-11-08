"""Rich owner screens retain exact financial values and existing action menus."""
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as owner
from telegram_membership_saas.modules.owner_setup import rich_admin_ui as ui
from telegram_membership_saas.modules.owner_setup.statistics import OwnerStatistics
from telegram_membership_saas.modules.owner_setup.subscribers import (
    SubscriberAccess,
    SubscriberDetail,
    SubscriberGrant,
    SubscriberInvoice,
    SubscriberInvoicePage,
    SubscriberPayment,
    SubscriberSubscription,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _stats():
    return OwnerStatistics(
        project_name="<Premium> & Shop", timezone="Europe/Moscow", period="30",
        since=date(2026, 11, 1), through=date(2026, 11, 9),
        payment_count=3, currency_totals=(
            ("RUB", Decimal("2650.0000"), 2),
            ("USD", Decimal("8.2500"), 1),
        ), total_customers=7, new_customers=2,
        active_paid_customers=4, expiring_paid_customers=1,
        active_manual_grants=2, waiting_review=3,
    )


def _card(*, allowed=True):
    resource = uuid4()
    return SubscriberDetail(
        customer_id=uuid4(), telegram_id=12345, username="vip",
        first_name="Buyer <Me>", project_timezone="Europe/Moscow",
        owner_only_actions=allowed,
        subscriptions=(
            SubscriberSubscription(
                plan_name="Premium <Plus> & 1", status="active",
                period_end=datetime(2026, 12, 10, 23, 0, tzinfo=UTC),
            ),
        ),
        payments=(
            SubscriberPayment(
                id=uuid4(), plan_name="Premium <Plus> & 1",
                amount="550.000000000000000000", currency="RUB",
                approved_at=datetime(2026, 11, 9, 22, 0, tzinfo=UTC),
            ),
        ),
        total_payments=1, history_page=0,
        grants=(
            SubscriberGrant(
                id=uuid4(), resource_id=resource,
                resource_name="Private <Channel> & Group",
                source="manual", valid_until=None,
            ),
        ),
        total_grants=1, grant_page=0,
        access=(
            SubscriberAccess(
                resource_name="Private <Channel> & Group",
                desired=True, actual="unknown",
            ),
        ),
        resources=((resource, "Private <Channel> & Group"),),
        total_invoices=6, pending_invoices=2,
    )


def _invoices():
    return SubscriberInvoicePage(
        customer_id=uuid4(), telegram_id=12345, username="vip",
        first_name="<Buyer>", project_timezone="Europe/Moscow",
        total=10, page=1,
        invoices=(
            SubscriberInvoice(
                id=uuid4(), plan_name="VIP <Pack> & Name",
                amount="7.000000000000000000", currency="EUR",
                created_at=datetime(2026, 11, 8, 23, 30, tzinfo=UTC),
                status="waiting_review",
            ),
            SubscriberInvoice(
                id=uuid4(), plan_name="Another plan",
                amount="200.000000000000000000", currency="RUB",
                created_at=datetime(2026, 11, 9, 21, 10, tzinfo=UTC),
                status="approved",
            ),
        ),
    )


@pytest.mark.parametrize(("lang", "expected", "excluded"), [
    ("ru", ("Статистика", "Подтверждённые оплаты", "Покупатели",
             "Активных платных подписчиков"), "Confirmed payments"),
    ("en", ("Statistics", "Confirmed payments", "Customers",
             "Active paying subscribers"), "Подтверждённые оплаты"),
])
def test_rich_statistics_preserves_currencies_counts_disclaimer_and_escaping(
    lang, expected, excluded,
):
    token = _language.set(lang)
    try:
        html = ui.rich_owner_statistics(_stats())
    finally:
        _language.reset(token)
    assert html.count("<table bordered compact>") == 4
    assert "&lt;Premium&gt; &amp; Shop" in html
    assert "<Premium>" not in html
    assert all(word in html for word in expected)
    assert excluded not in html
    for expected_value in ("2650", "8.25", "RUB", "USD", ">2<", ">1<", ">4<", ">3<"):
        assert expected_value in html
    assert "2650.0000" not in html
    assert "не банком" in html if lang == "ru" else "not independently bank-verified" in html


@pytest.mark.parametrize(("lang", "expected", "absent"), [
    ("ru", ("Карточка покупателя", "Подписки", "Оплаченные покупки",
             "Основания доступа", "Доступ к каналам", "Ручной",
             "не подтверждён"), "Access grants"),
    ("en", ("Customer card", "Subscriptions", "Confirmed purchases",
             "Access grants", "Channel access", "Manual",
             "not verified"), "Основания доступа"),
])
def test_rich_subscriber_card_exact_values_local_dates_and_html(
    lang, expected, absent,
):
    token = _language.set(lang)
    try:
        html = ui.rich_subscriber_card(_card(), who="vip <Owner> & buyers")
    finally:
        _language.reset(token)
    assert all(x in html for x in expected)
    assert absent not in html
    assert "&lt;Owner&gt; &amp; buyers" in html
    assert "Premium &lt;Plus&gt; &amp; 1" in html
    assert "Private &lt;Channel&gt; &amp; Group" in html
    assert "<Owner>" not in html
    assert "550 RUB" in html
    assert "550.000000000000000000" not in html
    assert "11.12.2026" in html  # Moscow local time
    assert "10.11.2026" in html
    assert "бессрочно" in html if lang == "ru" else "lifetime" in html
    assert html.count("<table bordered compact>") == 5
    assert "Telegram ID" in html and "12345" in html


@pytest.mark.parametrize(("lang", "status", "empty_text"), [
    ("ru", "На проверке", "Счетов пока нет."),
    ("en", "Under review", "No invoices yet."),
])
def test_rich_invoice_history_compact_rows_status_local_date_and_empty(
    lang, status, empty_text,
):
    data = _invoices()
    token = _language.set(lang)
    try:
        html = ui.rich_subscriber_invoices(data, who="Buyer <Me>")
        empty = ui.rich_subscriber_invoices(
            replace(data, total=0, page=0, invoices=()), who="Buyer",
        )
    finally:
        _language.reset(token)
    assert html.count("<tr>") == 3
    assert "<table bordered compact>" in html
    assert "7 EUR" in html and "200 RUB" in html
    assert status in html
    assert "Page 2 / 2" in html if lang == "en" else "Страница 2 / 2" in html
    assert "VIP &lt;Pack&gt; &amp; Name" in html
    assert "<Me>" not in html and "&lt;Me&gt;" in html
    assert "09.11.2026" in html and "10.11.2026" in html
    assert "7.000000000000000000" not in html
    assert empty_text in empty
    assert "<table" not in empty


async def test_rich_send_preserves_exact_keyboard_and_avoids_double_send(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    monkeypatch.setattr(
        ui, "InputRichMessage", lambda **kwargs: SimpleNamespace(**kwargs),
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Activate", callback_data="own:subactivate:abc"),
        InlineKeyboardButton(text="🗑 Revoke", callback_data="own:subcancel:abc"),
    ]])
    bot = MagicMock()
    bot.send_rich_message = AsyncMock()
    bot.send_message = AsyncMock()
    await ui.send_owner_rich(
        bot, chat_id=123, rich_html="<h3>Rich</h3>",
        classic_html="<b>Classic</b>", reply_markup=keyboard,
    )
    kwargs = bot.send_rich_message.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert kwargs["rich_message"].html == "<h3>Rich</h3>"
    assert kwargs["reply_markup"] is keyboard
    bot.send_message.assert_not_awaited()

    bot.send_rich_message.side_effect = ConnectionError("timeout after delivery")
    with pytest.raises(ConnectionError):
        await ui.send_owner_rich(
            bot, chat_id=123, rich_html="<h3>Rich</h3>",
            classic_html="<b>Classic</b>", reply_markup=keyboard,
        )
    bot.send_message.assert_not_awaited()


async def test_rich_invalid_html_fallback_and_opt_out(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    monkeypatch.setattr(
        ui, "InputRichMessage", lambda **kwargs: SimpleNamespace(**kwargs),
    )
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(side_effect=TelegramBadRequest(
        method=SendMessage(chat_id=1, text="bad"),
        message="Bad Request: can't parse rich message",
    ))
    bot.send_message = AsyncMock()
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Back", callback_data="own:dashboard"),
    ]])
    await ui.send_owner_rich(
        bot, chat_id=1, rich_html="<h3>Rich</h3>",
        classic_html="<b>Original classic</b>", reply_markup=markup,
    )
    bot.send_message.assert_awaited_once_with(
        1, "<b>Original classic</b>", parse_mode="HTML", reply_markup=markup,
    )
    bot.send_rich_message.reset_mock()
    bot.send_message.reset_mock()
    monkeypatch.setattr(ui, "rich_enabled", lambda: False)
    await ui.send_owner_rich(
        bot, chat_id=1, rich_html="<h3>Rich</h3>",
        classic_html="<b>Original classic</b>", reply_markup=markup,
    )
    bot.send_rich_message.assert_not_awaited()
    bot.send_message.assert_awaited_once()


async def test_unrelated_api_failure_does_not_send_fallback(monkeypatch):
    monkeypatch.setattr(ui, "rich_enabled", lambda: True)
    monkeypatch.setattr(
        ui, "InputRichMessage", lambda **kwargs: SimpleNamespace(**kwargs),
    )
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(side_effect=TelegramBadRequest(
        method=SendMessage(chat_id=1, text="bad"),
        message="Bad Request: chat not found",
    ))
    bot.send_message = AsyncMock()
    with pytest.raises(TelegramBadRequest):
        await ui.send_owner_rich(
            bot, chat_id=1, rich_html="<h3>Rich</h3>",
            classic_html="Classic", reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="Back", callback_data="own:dashboard"),
                ]],
            ),
        )
    bot.send_message.assert_not_awaited()


def _callback(value):
    return SimpleNamespace(
        data="own:" + value,
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        answer=AsyncMock(),
    )


def _router_callback(project):
    router = owner.create_owner_setup_router(
        MagicMock(), fixed_project_id=project,
    )
    return next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "owner_callback"
    )


async def test_three_connected_admin_routes_render_rich_without_changing_actions(
    monkeypatch,
):
    project, actor, cid = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(owner, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(owner, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(owner, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(owner, "read_owner_statistics", AsyncMock(return_value=_stats()))
    monkeypatch.setattr(owner, "subscriber_detail", AsyncMock(return_value=_card()))
    monkeypatch.setattr(
        owner, "list_subscriber_invoices", AsyncMock(return_value=_invoices()),
    )
    render = AsyncMock()
    monkeypatch.setattr(owner, "send_owner_rich", render)
    bot = MagicMock()
    bot.id = 4444
    handler = _router_callback(project)
    for command, fragment in (
        ("statistics:30", "Статистика"),
        ("subscriber:" + encode_uuid(cid), "Карточка покупателя"),
        ("subinvoices:" + encode_uuid(cid) + ":1", "Счета покупателя"),
    ):
        render.reset_mock()
        await handler(_callback(command), bot)
        render.assert_awaited_once()
        kwargs = render.await_args.kwargs
        assert kwargs["chat_id"] == 123
        assert fragment in kwargs["rich_html"]
        assert "<table bordered compact>" in kwargs["rich_html"]
        assert kwargs["classic_html"]
        callbacks = [
            button.callback_data
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row if button.callback_data
        ]
        if command.startswith("statistics:"):
            assert "own:statistics:7" in callbacks
            assert "own:statistics:30" in callbacks
        elif command.startswith("subscriber:"):
            assert "own:subscriber:" + encode_uuid(cid) in callbacks
            assert "own:subinvoices:" + encode_uuid(cid) + ":0" in callbacks
            assert "own:subcancel:" + encode_uuid(cid) in callbacks
        else:
            assert "own:invoice:" + encode_uuid(_invoices().invoices[0].id) not in callbacks
            assert "own:subscriber:" + encode_uuid(cid) in callbacks
            assert "own:subinvoices:" + encode_uuid(cid) + ":2" not in callbacks


async def test_admin_rich_stays_owner_scoped(monkeypatch):
    monkeypatch.setattr(owner, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(owner, "can_manage_project", AsyncMock(return_value=False))
    render = AsyncMock()
    monkeypatch.setattr(owner, "send_owner_rich", render)
    cb = _callback("subscriber:" + encode_uuid(uuid4()))
    await _router_callback(uuid4())(cb, MagicMock())
    render.assert_not_awaited()
    assert cb.answer.await_args.kwargs["show_alert"]
