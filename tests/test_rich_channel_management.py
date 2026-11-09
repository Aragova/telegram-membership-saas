"""Native Rich channel directory and per-channel statistics, with unchanged callbacks."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as owner
from telegram_membership_saas.modules.owner_setup import rich_admin_ui as ui
from telegram_membership_saas.modules.owner_setup.channel_management import ChannelInsights
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _metrics(*, paid=True):
    return ChannelInsights(
        resource_id=uuid4(), name="ONLY <YOU> & FULL",
        external_id="-100123456789", resource_type="telegram_channel",
        active_plans=4, paid_orders=1 if paid else 0,
        paid_customers=1 if paid else 0,
        active_paid_customers=1 if paid else 0,
        pending_invoices=6,
        sales_by_currency=(
            ("RUB", Decimal("500.000000"), 1),
            ("BTC", Decimal("0.00010000"), 1),
        ) if paid else (),
    )


@pytest.mark.parametrize(("lang", "title", "count_label"), [
    ("ru", "Мои каналы и группы", "Подключено"),
    ("en", "My channels and groups", "Connected"),
])
def test_rich_channel_directory_has_compact_counter_not_duplicate_channel_list(
    lang, title, count_label,
):
    token = _language.set(lang)
    try:
        html = ui.rich_channel_directory(48)
    finally:
        _language.reset(token)
    assert html.startswith("<h3>") and title in html
    assert "<table bordered compact>" in html
    assert "<th>" + count_label + "</th><td>48</td>" in html
    assert html.count("<table") == 1
    assert "<p>" in html


@pytest.mark.parametrize(("lang", "title", "subscribers"), [
    ("ru", "Статистика канала", "Активных платных подписчиков"),
    ("en", "Channel statistics", "Active paid subscribers"),
])
def test_rich_channel_card_preserves_real_counts_cryptocurrency_and_escaping(
    lang, title, subscribers,
):
    token = _language.set(lang)
    try:
        html = ui.rich_channel_card(_metrics(), members_text="39")
    finally:
        _language.reset(token)
    assert title in html and subscribers in html
    assert html.count("<table bordered compact>") == 2
    assert "ONLY &lt;YOU&gt; &amp; FULL" in html
    assert "ONLY <YOU> & FULL" not in html
    for val in ("39", ">4<", ">6<", "RUB", "BTC", ">500<", ">0.0001<"):
        assert val in html
    assert "0.00</td>" not in html
    assert "500.000000" not in html
    assert html.count("<tr>") == 9
    assert "🔒" in html
    assert "Confirmed purchases" in html if lang == "en" else "подтверждённые покупки" in html


def test_rich_channel_card_no_paid_sales_and_unavailable_telegram_members():
    card = _metrics(paid=False)
    html = ui.rich_channel_card(card, members_text="Недоступно")
    assert "Недоступно" in html
    assert "Нет подтверждённых продаж" in html
    assert "<table bordered compact>" in html
    assert html.count("<table") == 1
    assert "🔒" not in html


def _callback(action):
    return SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=123)),
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        answer=AsyncMock(),
    )


def _router(project):
    router = owner.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )


async def test_both_connected_channel_routes_send_rich_with_original_action_keys(
    monkeypatch,
):
    project, actor, channel = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(owner, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(owner, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(owner, "get_selected_project", AsyncMock(return_value=project))
    monkeypatch.setattr(owner, "setup_overview", AsyncMock(return_value=SetupOverview(
        project_id=project, project_name="VIP", timezone="Europe/Moscow",
        bots=(), plans=(), methods=(),
        resources=((channel, "ONLY <YOU> & FULL", "active"),),
    )))
    insight = _metrics()
    insight = ChannelInsights(
        resource_id=channel, name=insight.name, external_id=insight.external_id,
        resource_type=insight.resource_type, active_plans=insight.active_plans,
        paid_orders=insight.paid_orders, paid_customers=insight.paid_customers,
        active_paid_customers=insight.active_paid_customers,
        pending_invoices=insight.pending_invoices,
        sales_by_currency=insight.sales_by_currency,
    )
    monkeypatch.setattr(owner, "read_channel_insights", AsyncMock(return_value=insight))
    render = AsyncMock()
    monkeypatch.setattr(owner, "send_owner_rich", render)
    bot = MagicMock(id=123456)
    bot.get_chat_member_count = AsyncMock(return_value=39)
    handler = _router(project)

    await handler(_callback("resources"), bot)
    render.assert_awaited_once()
    args = render.await_args.kwargs
    assert "<table bordered compact>" in args["rich_html"]
    assert "Мои каналы и группы" in args["classic_html"]
    assert "Подключено: 1" in args["classic_html"]
    actions = [
        b.callback_data for row in args["reply_markup"].inline_keyboard for b in row
    ]
    assert "own:resourceview:" + encode_uuid(channel) in actions
    assert {"own:pick:channel", "own:pick:group", "own:resource:manual"} <= set(actions)

    render.reset_mock()
    await handler(_callback("resourceview:" + encode_uuid(channel)), bot)
    render.assert_awaited_once()
    args = render.await_args.kwargs
    assert args["rich_html"].count("<table bordered compact>") == 2
    assert "39" in args["rich_html"]
    assert "• RUB: 500 (1)" in args["classic_html"]
    assert "BTC" in args["rich_html"]
    assert bot.get_chat_member_count.await_args.kwargs["chat_id"] == -100123456789
    actions = [
        b.callback_data for row in args["reply_markup"].inline_keyboard for b in row
    ]
    assert "own:resources" in actions
    assert not any(x.startswith("own:resremoveask:") for x in actions)
