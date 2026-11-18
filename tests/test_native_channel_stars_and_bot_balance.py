"""Native channel Stars routes stay separate from bot Stars invoices and ledgers."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from test_channel_management_ui import _auth, _bot, _handler, _query

from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.owner_setup.native_channel_stars import (
    NATIVE_PERIOD_SECONDS,
    native_price_allowed,
)
from telegram_membership_saas.modules.telegram_commerce.service import CheckoutOption
from telegram_membership_saas.modules.telegram_commerce.storefront import (
    build_buyer_offers,
    offer_screen,
)


@pytest.mark.parametrize(("value", "unit", "amount", "allowed"), [
    (1, "month", "1", True),
    (1, "month", "10000", True),
    (1, "month", "0", False),
    (1, "month", "10001", False),
    (1, "month", "12.5", False),
    (30, "day", "100", False),
    (2, "month", "100", False),
])
def test_native_channel_star_constraints(value, unit, amount, allowed):
    assert native_price_allowed(
        duration_value=value, duration_unit=unit, amount=Decimal(amount),
    ) is allowed
    assert NATIVE_PERIOD_SECONDS == 2_592_000


def test_native_link_is_one_buyer_payment_button_not_a_bot_invoice():
    price_id, plan_id = uuid4(), uuid4()
    option = CheckoutOption(
        plan_id=plan_id, plan_name="Premium", duration_value=1,
        duration_unit="month", plan_price_id=price_id,
        currency_code="XTR", amount=Decimal("50"), currency_precision=0,
        payment_method_id=UUID(int=0), payment_method_name="⭐ Telegram Stars",
    )
    link = "https://t.me/+genuine_link_placeholder"
    offers = build_buyer_offers(
        (option,), native_links={price_id: link},
    )
    _, keyboard = offer_screen(offers, price_id=price_id)
    buttons = [item for row in keyboard.inline_keyboard for item in row]
    assert sum(item.url == link for item in buttons) == 1
    assert not any((item.callback_data or "").startswith("buy:") for item in buttons)

    classic = build_buyer_offers((option,))
    _, regular = offer_screen(classic, price_id=price_id)
    assert any((item.callback_data or "").startswith("buy:") for row in
               regular.inline_keyboard for item in row)


async def test_admin_dashboard_shows_two_rows_live_star_balance_and_actual_owner(monkeypatch):
    project, channel = uuid4(), uuid4()
    _auth(monkeypatch, project)
    from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
        ChannelFinance,
        DashboardFinances,
    )
    from telegram_membership_saas.modules.owner_setup.service import SetupOverview

    snapshot = SetupOverview(
        project_id=project, project_name="@premium0fans_bot",
        timezone="Europe/Moscow",
        bots=(("premium0fans_bot", "registered"),),
        resources=((channel, "Private VIP", "active"),),
        plans=((uuid4(), "Monthly", "active"),),
        methods=((uuid4(), "Manual", "active"),),
        live_bot_username="premium0fans_bot",
        sales_configured=True,
        bot_instances=((uuid4(), "premium0fans_bot", "registered"),),
    )
    monkeypatch.setattr(owner_ui, "setup_overview", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(owner_ui, "merchant_saas_trial_status", AsyncMock(
        return_value={"state": "exempt"},
    ))
    monkeypatch.setattr(owner_ui, "_project_saas_hold_state", AsyncMock(
        return_value="clear",
    ))
    finance = DashboardFinances(
        bot_stars="0", channels=(ChannelFinance(
            name="Private VIP", chat_id="-100111111",
            owner_label="@actual_owner · ID 12345",
            owner_deleted=False, stars_recipient="channel",
        ),),
    )
    monkeypatch.setattr(owner_ui, "read_dashboard_finances", AsyncMock(
        return_value=finance,
    ))
    send = AsyncMock()
    monkeypatch.setattr(owner_ui, "send_owner_rich", send)
    await _handler(project)(_query("dashboard"), _bot())
    message = send.await_args.kwargs
    assert "0 ⭐" in message["rich_html"]
    assert "@actual_owner" in message["rich_html"]
    assert "Недоступен через Bot API" in message["rich_html"]
    assert "⚪ Не проверен" in message["rich_html"]
    assert "ViMi Pay PRO" in message["rich_html"]
    # Exactly two status-grid rows: three checks, then two.
    first_grid = message["rich_html"].split("<h4>")[0]
    assert first_grid.count("<table bordered compact>") == 2
    assert first_grid.count("<tr>") == 2
    assert first_grid.count("<td>") == 5
    assert "Продажи доступны" in message["rich_html"]
    assert "Баланс бота" in message["classic_html"]
    buttons = [
        (button.text, button.callback_data)
        for row in message["reply_markup"].inline_keyboard
        for button in row
    ]
    assert len(buttons) == 12
    assert ("📢 Каналы", "own:resources") in buttons
    assert any(callback == "fs:home" for _, callback in buttons)
    assert ("📊 Статистика", "own:statistics:7") in buttons


async def test_dashboard_balance_unknown_is_not_zero_and_owner_deleted(monkeypatch):
    from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
        ChannelFinance,
        DashboardFinances,
    )
    from telegram_membership_saas.modules.owner_setup.dashboard_ui import (
        classic_dashboard,
        rich_dashboard,
    )
    data = dict(
        project_name="<VIP> & MORE", timezone="Europe/Moscow",
        trial_banner="", sales_status="⚠️ Нет продаж",
        readiness=(("Connected bot", True), ("Channel", False),
                   ("Plan", True), ("Method", False), ("Running", False)),
        finances=DashboardFinances(
            bot_stars=None,
            channels=(ChannelFinance(
                name="Old <Channel>", chat_id="-100444",
                owner_label="ID 100", owner_deleted=True,
                stars_recipient="unconfigured",
            ),),
        ),
    )
    classic = classic_dashboard(**data)
    html = rich_dashboard(**data)
    assert "Недоступен" in classic and "Баланс бота: 0" not in classic
    assert "Удалённый аккаунт" in classic
    assert "Old &lt;Channel&gt;" not in html or "<VIP>" not in html
    assert "&lt;VIP&gt; &amp; MORE" in html
    assert "🔒" not in html
    assert "Не готов" in html


async def test_bot_star_balance_live_zero_and_failed_are_distinct():
    from telegram_membership_saas.modules.owner_setup.dashboard_finances import _live_stars
    good = _bot()
    good.get_my_star_balance = AsyncMock(return_value=SimpleNamespace(
        amount=0, nanostar_amount=0,
    ))
    assert await _live_stars(good) == "0"
    good.get_my_star_balance.assert_awaited_once()
    bad = _bot()
    bad.get_my_star_balance = AsyncMock(side_effect=ValueError("API outage"))
    assert await _live_stars(bad) is None


def test_owner_identity_requires_creator_not_other_admins():
    from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
        _owner_from_admins,
    )
    admin = SimpleNamespace(status="administrator", user=SimpleNamespace(
        id=9, username="just_admin", first_name="Admin",
    ))
    creator = SimpleNamespace(status="creator", user=SimpleNamespace(
        id=123, username="legit_owner", first_name="Owner",
        last_name=None, is_deleted=False,
    ))
    assert _owner_from_admins([admin, creator]) == (
        "@legit_owner · ID 123", False,
    )
    assert _owner_from_admins([admin]) == (None, False)
    deleted = SimpleNamespace(status="creator", user=SimpleNamespace(
        id=777, username=None, is_deleted=True,
    ))
    assert _owner_from_admins([deleted]) == ("ID 777", True)
