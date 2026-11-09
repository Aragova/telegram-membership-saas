"""Connected-bot channel list and per-channel read-only statistics UI."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.channel_management import ChannelInsights
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _bot():
    bot = MagicMock()
    bot.id = 554400
    bot.send_message = AsyncMock()
    bot.get_chat_member_count = AsyncMock(return_value=248)
    return bot


def _query(action):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(id=123, username="owner", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(id=123, type="private")),
        answer=AsyncMock(),
    )


def _handler(project):
    router = ui.create_owner_setup_router(MagicMock(), fixed_project_id=project)
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )


def _auth(monkeypatch, project_id):
    actor = uuid4()
    monkeypatch.setattr(ui, "ensure_telegram_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(ui, "can_manage_project", AsyncMock(return_value=True))
    monkeypatch.setattr(ui, "get_selected_project", AsyncMock(return_value=project_id))
    return actor


def _metrics(resource_id, *, orders=0):
    return ChannelInsights(
        resource_id=resource_id, name="Private <VIP> & Gold",
        external_id="-1001122334455", resource_type="telegram_channel",
        active_plans=2, paid_orders=orders, paid_customers=1 if orders else 0,
        active_paid_customers=1 if orders else 0,
        pending_invoices=3,
        sales_by_currency=(("RUB", Decimal("550"), orders),) if orders else (),
    )


async def test_channel_list_has_actions_and_individual_channel_navigation(monkeypatch):
    project, channel = uuid4(), uuid4()
    _auth(monkeypatch, project)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=SetupOverview(
        project_id=project, project_name="VIP bot", timezone="Europe/Moscow",
        bots=(), plans=(), methods=(),
        resources=((channel, "Private <VIP> & Gold", "active"),),
    )))
    bot = _bot()
    token = _language.set("ru")
    try:
        await _handler(project)(_query("resources"), bot)
    finally:
        _language.reset(token)
    message = bot.send_message.await_args
    assert "Мои каналы" in message.args[1]
    assert "Подключено: 1" in message.args[1]
    actions = [btn.callback_data for row in message.kwargs["reply_markup"].inline_keyboard
               for btn in row]
    assert "own:resourceview:" + encode_uuid(channel) in actions
    assert {"own:pick:channel", "own:pick:group", "own:resource:manual"} <= set(actions)


async def test_channel_card_tg_count_is_distinct_from_paid_customers(monkeypatch):
    project, channel = uuid4(), uuid4()
    _auth(monkeypatch, project)
    read = AsyncMock(return_value=_metrics(channel))
    monkeypatch.setattr(ui, "read_channel_insights", read)
    bot = _bot()
    await _handler(project)(_query("resourceview:" + encode_uuid(channel)), bot)
    text = bot.send_message.await_args.args[1]
    assert "Private &lt;VIP&gt; &amp; Gold" in text
    assert "248" in text
    assert "Активных платных подписчиков: 0" in text
    assert "Неоплаченных заявок: 3" in text
    assert bot.get_chat_member_count.await_args.kwargs["chat_id"] == -1001122334455
    buttons = [
        btn.callback_data for row in
        bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert "own:resremoveask:" + encode_uuid(channel) in buttons
    assert "own:resources" in buttons


async def test_paid_channel_cannot_be_removed_even_by_old_callback(monkeypatch):
    project, channel = uuid4(), uuid4()
    _auth(monkeypatch, project)
    read = AsyncMock(return_value=_metrics(channel, orders=1))
    remove = AsyncMock()
    monkeypatch.setattr(ui, "read_channel_insights", read)
    monkeypatch.setattr(ui, "remove_channel_without_paid_purchases", remove)
    bot = _bot()
    await _handler(project)(_query("resourceview:" + encode_uuid(channel)), bot)
    buttons = [
        btn.callback_data for row in
        bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert not any(x.startswith("own:resremoveask:") for x in buttons)
    query = _query("resremoveok:" + encode_uuid(channel))
    await _handler(project)(query, bot)
    remove.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_unpaid_channel_deletion_confirms_and_hides_plans(monkeypatch):
    project, channel = uuid4(), uuid4()
    actor = _auth(monkeypatch, project)
    monkeypatch.setattr(ui, "read_channel_insights", AsyncMock(
        return_value=_metrics(channel),
    ))
    remove = AsyncMock(return_value=("Private VIP", 2))
    monkeypatch.setattr(ui, "remove_channel_without_paid_purchases", remove)
    monkeypatch.setattr(ui, "setup_overview", AsyncMock(return_value=SetupOverview(
        project_id=project, project_name="VIP", timezone="Europe/Moscow",
        bots=(), plans=(), methods=(), resources=(),
    )))
    bot = _bot()
    handler = _handler(project)
    await handler(_query("resremoveask:" + encode_uuid(channel)), bot)
    assert "Неоплаченные счета сохранятся" in bot.send_message.await_args.args[1]
    remove.assert_not_awaited()
    await handler(_query("resremoveok:" + encode_uuid(channel)), bot)
    remove.assert_awaited_once()
    assert remove.await_args.kwargs == {
        "actor_id": actor, "project_id": project, "resource_id": channel,
    }
    rendered = [call.args[1] for call in bot.send_message.await_args_list]
    assert any("Скрыто тарифов: 2" in text for text in rendered)
