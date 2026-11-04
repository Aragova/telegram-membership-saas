"""Native Telegram menus and no-dead-end buyer/owner navigation contracts."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules.owner_setup import service as owner_service
from telegram_membership_saas.modules.telegram_commerce import handlers as commerce
from telegram_membership_saas.modules.telegram_commerce.account import (
    BuyerAccount,
    BuyerRequest,
    BuyerSubscription,
    account_home_screen,
    account_list_screen,
    load_buyer_account,
    support_screen,
)
from telegram_membership_saas.telegram_menus import (
    commands_for,
    install_public_commands,
    refresh_user_commands,
    set_chat_commands,
)


def _buttons(keyboard):
    return [
        (b.text, b.callback_data)
        for row in keyboard.inline_keyboard for b in row
    ]


def _bot():
    bot = MagicMock()
    bot.id = 9000
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.set_my_commands = AsyncMock()
    return bot


def test_roles_offer_real_commands_and_localized_descriptions():
    customer = commands_for("buyer", "en")
    assert [c.command for c in customer] == [
        "menu", "subscriptions", "support", "language", "cancel",
    ]
    assert "My purchases" in customer[1].description
    assert "Мои покупки" in commands_for("buyer", "ru")[1].description
    assert [c.command for c in commands_for("owner", "en")] == [
        "menu", "plans", "payments", "help", "language", "cancel",
    ]
    assert [c.command for c in commands_for("central", "en")] == [
        "menu", "bots", "help", "language",
    ]
    assert commands_for("platform", "en")[0].command == "platform"
    assert all(not c.command.startswith("/") for c in customer)


async def test_native_menu_default_and_language_specific_chat_override():
    bot = _bot()
    await install_public_commands(bot, customer=True)
    assert bot.set_my_commands.await_count == 2
    first, second = bot.set_my_commands.await_args_list
    assert first.kwargs["scope"].type == "all_private_chats"
    assert first.kwargs["commands"][0].description == "🏠 Home and plans"
    assert second.kwargs["language_code"] == "ru"
    assert second.kwargs["commands"][0].description == "🏠 Главное меню и тарифы"

    await set_chat_commands(bot, chat_id=711, role="owner", language="en")
    command = bot.set_my_commands.await_args
    assert command.kwargs["scope"].type == "chat"
    assert command.kwargs["scope"].chat_id == 711
    assert command.kwargs["commands"][1].command == "plans"
    assert "Manage plans" in command.kwargs["commands"][1].description


async def test_central_platform_owner_override_uses_own_chat_only(monkeypatch):
    from telegram_membership_saas import telegram_menus

    bot = _bot()
    monkeypatch.setattr(
        telegram_menus, "get_settings",
        lambda: SimpleNamespace(platform_owner_telegram_id=777),
    )
    await refresh_user_commands(
        bot, MagicMock(), chat_id=777, language="en",
    )
    assert bot.set_my_commands.await_args.kwargs["commands"][0].command == "platform"
    await refresh_user_commands(
        bot, MagicMock(), chat_id=778, language="ru",
    )
    assert bot.set_my_commands.await_args.kwargs["commands"][0].command == "menu"


def test_empty_account_is_useful_and_never_shows_a_fake_subscription():
    account = BuyerAccount()
    token = _language.set("en")
    try:
        text, keyboard = account_home_screen(account)
        assert "My purchases" in text
        assert "Subscriptions: 0" in text
        assert "Payment requests: 0" in text
        assert ("💎 Browse plans", "account:catalog") in _buttons(keyboard)

        text, keyboard = account_list_screen(account, section="reqs", page=0)
        assert "Nothing to show here yet" in text
        assert ("⬅️ My purchases", "account:home") in _buttons(keyboard)
        text, keyboard = support_screen()
        assert "not configured a dedicated support contact" in text
        assert not any("@" in label for label, _ in _buttons(keyboard))
    finally:
        _language.reset(token)


def test_request_account_can_resume_receipt_without_approving_or_canceling():
    request_id = uuid4()
    account = BuyerAccount(
        requests_count=2,
        requests=(
            BuyerRequest(
                id=request_id, name="Premium <VIP>",
                status="waiting_confirmation",
                amount=Decimal("550"), currency="RUB",
            ),
            BuyerRequest(
                id=uuid4(), name="Annual",
                status="waiting_review",
                amount=Decimal("7"), currency="USD",
            ),
        ),
    )
    token = _language.set("en")
    try:
        text, keyboard = account_list_screen(account, section="reqs", page=0)
        assert "Premium &lt;VIP&gt;" in text
        assert "Awaiting receipt" in text
        assert "Under review" in text
        actions = [data for _, data in _buttons(keyboard)]
        assert len([x for x in actions if x.startswith("paid:")]) == 1
        assert not any(x.startswith(("approve:", "cancel:", "buy:")) for x in actions)
        assert "account:home" in actions
        assert "account:catalog" in actions
        assert "account:reqs:1" not in actions
    finally:
        _language.reset(token)


def test_subscription_history_paginates_and_formats_expiration():
    ended = datetime(2026, 12, 12, 10, tzinfo=UTC)
    account = BuyerAccount(
        subscriptions_count=11, timezone="Europe/Moscow",
        subscriptions=(
            BuyerSubscription(
                name="Channel & Co", status="active", ends_at=ended,
            ),
            BuyerSubscription(name="Lifetime", status="expired", ends_at=None),
        ),
    )
    token = _language.set("en")
    try:
        text, keyboard = account_list_screen(account, section="subs", page=1)
        assert "Channel &amp; Co" in text
        assert "12.12.2026 13:00 (Europe/Moscow)" in text
        actions = [data for _, data in _buttons(keyboard)]
        assert "account:subs:0" in actions
        assert "account:subs:2" in actions
    finally:
        _language.reset(token)


async def test_unknown_buyer_does_not_create_an_account():
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(),
        add=MagicMock(),
    )
    result = await load_buyer_account(
        session, project_id=uuid4(), telegram_user_id=111,
    )
    assert result == BuyerAccount()
    session.scalar.assert_awaited_once()
    session.execute.assert_not_awaited()
    session.add.assert_not_called()


async def test_account_callback_does_not_modify_payments(monkeypatch):
    @asynccontextmanager
    async def read_scope(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(commerce, "system_project_session_scope", read_scope)
    account_reader = AsyncMock(return_value=BuyerAccount())
    monkeypatch.setattr(commerce, "load_buyer_account", account_reader)
    bot = _bot()
    query = SimpleNamespace(
        data="account:home",
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=200),
                                message_id=7),
        from_user=SimpleNamespace(id=200, username="buyer"),
        answer=AsyncMock(),
    )
    router = commerce.create_telegram_commerce_router(None, project_id=uuid4())
    callback = next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "account_callback"
    )
    token = _language.set("en")
    try:
        await callback(query, bot)
        assert "My purchases" in bot.edit_message_text.await_args.kwargs["text"]
        assert bot.send_message.await_count == 0
        account_reader.assert_awaited_once()
        query.answer.assert_awaited_once()

        query.message.chat.id = 333  # Callback from someone else's screen
        bot.edit_message_text.reset_mock()
        account_reader.reset_mock()
        await callback(query, bot)
        assert query.answer.await_args.kwargs["show_alert"] is True
        bot.edit_message_text.assert_not_awaited()
        account_reader.assert_not_awaited()
    finally:
        _language.reset(token)


async def test_cancel_owner_setup_invalidates_prompt_but_not_project(monkeypatch):
    project_id, actor_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    state = SimpleNamespace(
        step="price_amount", payload={"plan_id": "secret"},
        generation="oldgeneration", prompt_message_id=99,
        selected_project_id=project_id,
        expires_at=now + timedelta(minutes=10),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=state))

    @asynccontextmanager
    async def owner_scope(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(owner_service, "tenant_session_scope", owner_scope)
    changed = await owner_service.cancel_owner_flow(
        MagicMock(), actor_id=actor_id, bot_scope_id=9000,
    )
    assert changed is True
    assert state.step is None
    assert state.prompt_message_id is None
    assert state.generation is None
    assert state.payload == {}
    assert state.selected_project_id == project_id
    assert state.expires_at <= datetime.now(UTC)
    assert session.get.await_args.args[1] == (actor_id, 9000)


def test_checkout_has_return_navigation_and_no_implicit_cancel():
    request_id = uuid4()
    keyboard = commerce._checkout_keyboard(request_id)
    labels = _buttons(keyboard)
    assert ("🧾 Мои покупки", "account:home") in labels
    assert ("💎 К тарифам", "account:catalog") in labels
    assert labels[1][0] == "Отменить заявку"
