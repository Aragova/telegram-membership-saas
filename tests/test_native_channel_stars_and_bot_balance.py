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
        bot_running=True,
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
            owner_username="actual_owner", owner_user_id=12345,
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
    assert '<a href="https://t.me/actual_owner">@actual_owner</a>' in message[
        "rich_html"
    ]
    assert "ID 12345" not in message["rich_html"]
    assert "Получатель звёзд" not in message["rich_html"]
    assert "Баланс канала" not in message["rich_html"]
    assert "Вывод" not in message["rich_html"]
    assert "Доступно к выводу" not in message["rich_html"]
    assert "ViMi Pay PRO" in message["rich_html"]
    # Exactly two 2-cell rows: four meaningful checks, no self-bot tautology.
    first_grid = message["rich_html"].split("<h4>")[0]
    assert first_grid.count("<table bordered compact>") == 2
    assert first_grid.count("<tr>") == 2
    assert first_grid.count("<td>") == 4
    assert "Собственный бот" not in first_grid
    assert "🟢" in first_grid and "ON" in first_grid
    assert "Подключено" not in first_grid
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
        readiness=(("Channel", False), ("Plan", True),
                   ("Method", False), ("Running", False)),
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
    assert "OFF" in html
    for output in (classic, html):
        assert "Баланс канала" not in output
        assert "Вывод" not in output
        assert "Channel balance" not in output
        assert "Withdrawal" not in output
    assert "Не готов" not in html


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


def test_owner_link_uses_real_username_not_internal_id():
    from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
        ChannelFinance,
        DashboardFinances,
    )
    from telegram_membership_saas.modules.owner_setup.dashboard_ui import (
        classic_dashboard,
        rich_dashboard,
    )

    base = dict(
        project_name="@premium0fans_bot", timezone="Europe/Moscow",
        trial_banner="", sales_status="✅ Продажи доступны",
        readiness=(("Канал / группа", True), ("Тариф", True),
                   ("Способ оплаты", True), ("Бот работает", False)),
    )
    channel = ChannelFinance(
        name="VIP", chat_id="-10011", owner_label="@kostik · ID 321",
        owner_deleted=False, stars_recipient="channel",
        owner_username="kostik", owner_user_id=321,
    )
    for render in (classic_dashboard, rich_dashboard):
        html = render(**base, finances=DashboardFinances(
            bot_stars="0", channels=(channel,),
        ))
        assert '<a href="https://t.me/kostik">@kostik</a>' in html
        assert "ID 321" not in html
        assert "Получатель звёзд" not in html
        assert "Владелец" in html
        assert "⭐" in html
        assert "🟢" in html and "OFF" in html
        assert "Подключено" not in html
        assert "<b>@premium0fans_bot</b>" in html
        assert "🌍" in html and "Europe/Moscow" in html
    rich = rich_dashboard(**base, finances=DashboardFinances(
        bot_stars="0", channels=(channel,),
    ))
    assert rich.split("<h4>")[0].count("<td>") == 4
    assert rich.split("<h4>")[0].count("<tr>") == 2


def test_owner_without_username_gets_clickable_telegram_mention_and_bot_recipient():
    from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
        ChannelFinance,
        DashboardFinances,
    )
    from telegram_membership_saas.modules.owner_setup.dashboard_ui import rich_dashboard

    def render(channel):
        return rich_dashboard(
            project_name="Bot", timezone="Europe/Moscow", trial_banner="",
            readiness=(("Channel", True), ("Plan", True),
                       ("Payment", True), ("Running", True)),
            sales_status="", finances=DashboardFinances(
                bot_stars="0", channels=(channel,),
            ),
        )
    channel = ChannelFinance(
        name="VIP", chat_id="-10011", owner_label="Анатолий · ID 321",
        owner_deleted=False, stars_recipient="bot", owner_user_id=321,
    )
    html = render(channel)
    assert '<a href="tg://user?id=321">Анатолий</a>' in html
    assert "Получатель звёзд" in html
    assert "🤖 Бот" in html
    assert "ID 321" not in html

    deleted = ChannelFinance(
        name="VIP", chat_id="-10011", owner_label="ID 321",
        owner_deleted=True, stars_recipient="channel", owner_user_id=321,
    )
    deleted_html = render(deleted)
    assert "Удалённый аккаунт" in deleted_html
    assert 'href="tg://user?id=321"' not in deleted_html


def test_running_status_reads_real_worker_heartbeat_not_invoice_configuration():
    from telegram_membership_saas.modules.owner_setup.handlers import _readiness_steps
    from telegram_membership_saas.modules.owner_setup.service import SetupOverview

    overview = SetupOverview(
        project_id=uuid4(), project_name="Bot", timezone="Europe/Moscow",
        bots=(("bot", "registered"),), resources=(), plans=(), methods=(),
        sales_configured=False, live_bot_username=None, bot_running=True,
    )
    checks = _readiness_steps(overview)
    assert len(checks) == 4
    assert checks[-1][1] is True
    assert all("Собственный бот" not in label for label, _ in checks)


@pytest.mark.parametrize(("status", "invites", "restrictions", "expected"), [
    ("administrator", True, True, True),
    ("administrator", True, False, False),
    ("administrator", False, True, False),
    ("member", True, True, False),
    ("creator", False, False, True),
])
async def test_channel_status_requires_live_bot_admin_rights(
    status, invites, restrictions, expected,
):
    from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
        _live_resource_rights,
    )
    bot = _bot()
    bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(
        status=status, can_invite_users=invites,
        can_restrict_members=restrictions,
    ))
    assert await _live_resource_rights(bot, "-1001234") is expected
    bot.get_chat_member.assert_awaited_once_with(
        chat_id=-1001234, user_id=bot.id,
    )


def test_stale_active_channel_does_not_display_on_without_live_permissions():
    from telegram_membership_saas.modules.owner_setup.handlers import _readiness_steps
    from telegram_membership_saas.modules.owner_setup.service import SetupOverview

    snapshot = SetupOverview(
        project_id=uuid4(), project_name="Bot", timezone="Europe/Moscow",
        bots=(("bot", "registered"),),
        resources=((uuid4(), "VIP", "active"),), plans=(), methods=(),
        bot_running=True,
    )
    assert _readiness_steps(snapshot)[0][1] is True
    assert _readiness_steps(snapshot, channel_verified=False)[0][1] is False
    assert _readiness_steps(snapshot, channel_verified=True)[0][1] is True
