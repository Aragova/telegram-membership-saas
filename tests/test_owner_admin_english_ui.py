"""Connected owner administration renders translated screens with stable actions."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.owner_setup.service import SetupOverview
from telegram_membership_saas.modules.telegram_commerce import handlers as checkout_ui
from telegram_membership_saas.modules.telegram_commerce.service import ReviewItem


def _overview(project_id):
    return SetupOverview(
        project_id=project_id, project_name="Original channel name",
        timezone="Europe/Moscow",
        bots=(("connected_owner_bot", "registered"),),
        resources=((uuid4(), "My premium channel", "active"),),
        plans=((uuid4(), "Premium Plan", "active"),),
        methods=((uuid4(), "Bank transfer", "active"),),
        sales_configured=True,
    )


async def _owner_action(monkeypatch, action, *, setup=None, methods=None):
    project_id = uuid4()
    actor_id = uuid4()
    monkeypatch.setattr(
        owner_ui, "ensure_telegram_actor", AsyncMock(return_value=actor_id),
    )
    monkeypatch.setattr(
        owner_ui, "can_manage_project", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        owner_ui, "get_selected_project", AsyncMock(return_value=project_id),
    )
    monkeypatch.setattr(
        owner_ui, "setup_overview", AsyncMock(
            return_value=setup or _overview(project_id),
        ),
    )
    monkeypatch.setattr(
        owner_ui, "list_owner_methods", AsyncMock(return_value=methods or []),
    )
    router = owner_ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=project_id,
    )
    handler = next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "owner_callback"
    )
    query = SimpleNamespace(
        data="own:" + action,
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=SimpleNamespace(
            id=12345, username="owner", first_name="Owner",
        ),
        answer=AsyncMock(),
    )
    bot = MagicMock()
    bot.id = 23456
    bot.send_message = AsyncMock()
    await handler(query, bot)
    return bot, query


async def test_english_resources_and_plans(monkeypatch):
    token = _language.set("en")
    try:
        bot, query = await _owner_action(monkeypatch, "resources")
        text = bot.send_message.await_args.args[1]
        assert "My channels and groups" in text
        assert "Select a channel" in text
        assert "Connected: 1" in text
        buttons = [
            b for row in bot.send_message.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard for b in row
        ]
        assert any(b.text == "➕ Add channel" and
                   b.callback_data == "own:pick:channel" for b in buttons)
        assert any("My premium channel" in b.text and
                   b.callback_data.startswith("own:resourceview:")
                   for b in buttons)
        query.answer.assert_awaited()

        bot, _ = await _owner_action(monkeypatch, "plans")
        text = bot.send_message.await_args.args[1]
        assert "<b>Plans</b>" in text
        assert "Premium Plan" in text
        labels = [
            b.text for row in bot.send_message.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard for b in row
        ]
        assert "➕ Create plan" in labels
    finally:
        _language.reset(token)


async def test_english_empty_payment_methods(monkeypatch):
    token = _language.set("en")
    try:
        bot, _ = await _owner_action(monkeypatch, "methods")
        text = bot.send_message.await_args.args[1]
        assert "Payment methods" in text
        assert "No payment methods yet" in text
        labels = [
            b.text for row in bot.send_message.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard for b in row
        ]
        assert "➕ Add method" in labels
    finally:
        _language.reset(token)


def test_payment_review_english_with_same_callbacks():
    item = ReviewItem(
        request_id=uuid4(), customer_telegram_user_id=12345,
        customer_username="demo", plan_name="Named by Owner",
        amount=Decimal("10"), currency_code="USD", payment_method_name="Bank transfer",
        status="waiting_review",
    )
    token = _language.set("en")
    try:
        text = checkout_ui._review_text(item)
        assert "Payment pending review" in text
        assert "Customer: @demo" in text
        assert "Named by Owner" in text
        assert "Confirm the bank transfer" in text
        keyboard = checkout_ui._review_keyboard(item.request_id)
        buttons = keyboard.inline_keyboard[0]
        assert buttons[0].text == "✅ Approve"
        assert buttons[0].callback_data.startswith("approve:")
        assert buttons[1].text == "Reject"
        assert buttons[1].callback_data.startswith("reject:")
    finally:
        _language.reset(token)
    assert "Заявка на подтверждение" in checkout_ui._review_text(item)
