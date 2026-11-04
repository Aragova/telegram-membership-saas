"""The connected bot drives owner setup and reviews; central stays token-only."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.modules.owner_setup import handlers as ui
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.owner_setup.tenant_admin import (
    MethodInfo,
    PlanInfo,
    PriceInfo,
)
from telegram_membership_saas.modules.telegram_commerce.service import ReviewItem


def _bot():
    bot = MagicMock()
    bot.id = 987654321
    bot.send_message = AsyncMock()
    return bot


def _query(action):
    return SimpleNamespace(
        data="own:" + action,
        from_user=SimpleNamespace(
            id=7777, username="owner", first_name="Owner",
        ),
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        answer=AsyncMock(),
    )


def _callback(project_id):
    router = ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project_id,
    )
    return next(
        item.callback for item in router.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )


def _auth(monkeypatch, project_id):
    actor_id = uuid4()
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    monkeypatch.setattr(
        ui, "can_manage_project", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        ui, "get_selected_project", AsyncMock(return_value=project_id),
    )
    return actor_id


def _snapshot(project_id, *, has_channel=False, has_method=False, has_plan=False):
    return SetupOverview(
        project_id=project_id, project_name="Premium E2E",
        timezone="Europe/Moscow",
        bots=(("rus_chan_prem_bot", "registered"),),
        resources=((uuid4(), "private channel", "active"),) if has_channel else (),
        plans=((uuid4(), "VIP", "active"),) if has_plan else (),
        methods=((uuid4(), "SBP", "active"),) if has_method else (),
        bot_runtime_label="🟡 Настройка", sales_configured=has_plan,
    )


async def test_admin_dashboard_guides_channel_then_payment_then_plans(monkeypatch):
    project = uuid4()
    _auth(monkeypatch, project)
    overview = AsyncMock(return_value=_snapshot(project))
    monkeypatch.setattr(ui, "setup_overview", overview)
    handler = _callback(project)
    bot = _bot()
    for snapshot, expected in [
        (_snapshot(project), "own:resources"),
        (_snapshot(project, has_channel=True), "own:methods"),
        (_snapshot(project, has_channel=True, has_method=True), "own:plans"),
    ]:
        overview.return_value = snapshot
        await handler(_query("dashboard"), bot)
        sent = bot.send_message.await_args
        assert "Панель администратора" in sent.args[1]
        first = sent.kwargs["reply_markup"].inline_keyboard[0][0]
        assert first.callback_data == expected
        assert "own:reviews" in [
            b.callback_data
            for row in sent.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]


async def test_owner_method_screen_shows_associated_currencies(monkeypatch):
    project, method = uuid4(), uuid4()
    _auth(monkeypatch, project)
    info = MethodInfo(method, "PayPal", "active", ("EUR", "USD"))
    monkeypatch.setattr(
        ui, "list_owner_methods", AsyncMock(return_value=(info,)),
    )
    monkeypatch.setattr(
        ui, "get_owner_method_info", AsyncMock(return_value=info),
    )
    bot = _bot()
    await _callback(project)(_query("methods"), bot)
    sent = bot.send_message.await_args
    assert "EUR, USD" in sent.args[1]
    assert "PayPal" in sent.args[1]
    buttons = [
        b.callback_data for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "own:method:" + method.hex in buttons
    assert "own:newmethod" in buttons

    await _callback(project)(_query("method:" + method.hex), bot)
    sent = bot.send_message.await_args
    callbacks = [
        b.callback_data for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "own:methodlink:" + method.hex in callbacks
    assert "own:methodconfirmoff:" + method.hex in callbacks


async def test_owner_can_attach_currency_without_changing_other_links(monkeypatch):
    project, method = uuid4(), uuid4()
    actor = _auth(monkeypatch, project)
    monkeypatch.setattr(
        ui, "get_owner_method_info",
        AsyncMock(return_value=MethodInfo(
            method, "PayPal", "active", ("EUR", "USD"),
        )),
    )
    attach = AsyncMock(return_value=True)
    monkeypatch.setattr(ui, "attach_owner_method_currency", attach)
    bot = _bot()
    await _callback(project)(
        _query("methodattach:" + method.hex + ":RUB"), bot,
    )
    assert attach.await_args.kwargs == {
        "actor_id": actor, "project_id": project,
        "method_id": method, "currency_code": "RUB",
    }
    assert "Валюта подключена" in bot.send_message.await_args_list[0].args[1]


async def test_payment_method_disable_requires_confirmation(monkeypatch):
    project, method = uuid4(), uuid4()
    _auth(monkeypatch, project)
    update = AsyncMock()
    monkeypatch.setattr(ui, "change_owner_method_status_guarded", update)
    monkeypatch.setattr(
        ui, "get_owner_method_info",
        AsyncMock(return_value=MethodInfo(
            method, "PayPal", "active", ("RUB",),
            revision="aabbccddeeff",
        )),
    )
    bot = _bot()
    await _callback(project)(_query("methodconfirmoff:" + method.hex), bot)
    update.assert_not_awaited()
    sent = bot.send_message.await_args
    assert "Отключить способ" in sent.args[1]
    from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid
    assert "own:methodoff:" + encode_uuid(method) + ":aabbccddeeff" in [
        b.callback_data for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]


async def test_plan_card_shows_price_and_missing_payment_method(monkeypatch):
    project, plan = uuid4(), uuid4()
    _auth(monkeypatch, project)
    monkeypatch.setattr(
        ui, "get_owner_plan_info", AsyncMock(return_value=PlanInfo(
            id=plan, name="Premium E2E", status="draft",
            duration_value=1, duration_unit="month", resource_name="VIP Channel",
            prices=(
                PriceInfo("RUB", Decimal("10"), 2, 1),
                PriceInfo("USD", Decimal("7"), 2, 0),
            ),
        )),
    )
    monkeypatch.setattr(
        ui, "owner_available_currencies", AsyncMock(return_value=("RUB", "USD")),
    )
    bot = _bot()
    await _callback(project)(_query("plan:" + plan.hex), bot)
    sent = bot.send_message.await_args
    assert "VIP Channel" in sent.args[1]
    assert "нужен способ оплаты" in sent.args[1]
    assert "оплата настроена" in sent.args[1]
    callbacks = [
        b.callback_data for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "own:newmethod" in callbacks
    assert any(value.startswith("own:publish:")
               for value in callbacks)


async def test_owner_review_inbox_uses_original_approved_payment_buttons(monkeypatch):
    project = uuid4()
    actor = _auth(monkeypatch, project)
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def project_scope(_factory, selected_project):
        assert selected_project == project
        yield MagicMock()

    monkeypatch.setattr(ui, "system_project_session_scope", project_scope)
    review = ReviewItem(
        request_id=uuid4(), customer_telegram_user_id=999,
        customer_username="buyer", plan_name="Premium E2E",
        amount=Decimal("10"), currency_code="RUB",
        payment_method_name="SBP", status="waiting_review",
    )
    listing = AsyncMock(return_value=(review,))
    monkeypatch.setattr(ui, "list_pending_reviews_for_actor", listing)
    bot = _bot()
    await _callback(project)(_query("reviews"), bot)
    assert listing.await_args.kwargs["project_id"] == project
    assert listing.await_args.kwargs["actor_telegram_user_id"] == 7777
    assert actor is not None
    assert bot.send_message.await_count == 3
    buttons = bot.send_message.await_args_list[1].kwargs[
        "reply_markup"
    ].inline_keyboard
    choices = [b.callback_data for row in buttons for b in row]
    assert "approve:" in choices[0]
    assert "reject:" in choices[1]


async def test_central_cannot_open_tenant_review_inbox(monkeypatch):
    monkeypatch.setattr(
        ui, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    central = ui.create_owner_setup_router(MagicMock())
    callback = next(
        item.callback for item in central.callback_query.handlers
        if item.callback.__name__ == "owner_callback"
    )
    query = _query("reviews")
    await callback(query, _bot())
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_incompatible_published_currency_guides_owner_back_to_payment(monkeypatch):
    # A plan remains published when its last matching payment method is disabled.
    from dataclasses import replace

    project = uuid4()
    _auth(monkeypatch, project)
    snapshot = replace(
        _snapshot(project, has_channel=True, has_method=True, has_plan=True),
        sales_configured=False,
    )
    assert ui._next_setup_step(snapshot)[1] == "methods"
    monkeypatch.setattr(
        ui, "setup_overview", AsyncMock(return_value=snapshot),
    )
    bot = _bot()
    await _callback(project)(_query("readiness"), bot)
    sent = bot.send_message.await_args
    callbacks = [
        b.callback_data for row in sent.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "own:methods" in callbacks
    assert "own:readiness" not in callbacks


async def test_method_list_paginates_and_deep_link_opens_offscreen_item(monkeypatch):
    project = uuid4()
    actor = _auth(monkeypatch, project)
    first = tuple(
        MethodInfo(uuid4(), "Old method " + str(n), "disabled", ("RUB",))
        for n in range(8)
    )
    latest = MethodInfo(uuid4(), "New method", "active", ("USD",))
    listing = AsyncMock(side_effect=[
        first, (latest,),
    ])
    monkeypatch.setattr(ui, "list_owner_methods", listing)
    lookup = AsyncMock(return_value=latest)
    monkeypatch.setattr(ui, "get_owner_method_info", lookup)
    bot = _bot()
    handler = _callback(project)
    await handler(_query("methods"), bot)
    controls = [
        b.callback_data for row in
        bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "own:methodpage:1" in controls
    await handler(_query("methodpage:1"), bot)
    controls = [
        b.callback_data for row in
        bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert "own:method:" + latest.id.hex in controls
    await handler(_query("method:" + latest.id.hex), bot)
    assert lookup.await_args.kwargs == {
        "actor_id": actor, "project_id": project,
        "method_id": latest.id,
    }
    assert "New method" in bot.send_message.await_args.args[1]
