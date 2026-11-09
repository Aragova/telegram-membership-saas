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
from telegram_membership_saas.support_links import SUPPORT_URL
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


def test_roles_offer_three_useful_commands_without_duplicates():
    expected_connected = ["menu", "subscription", "support"]
    expected_central = ["menu", "bots", "help"]
    for role in ("buyer", "owner"):
        for language in ("ru", "en"):
            commands = commands_for(role, language)
            assert [command.command for command in commands] == expected_connected
            assert len({command.command for command in commands}) == 3
    for role in ("central", "platform"):
        for language in ("ru", "en"):
            commands = commands_for(role, language)
            assert [command.command for command in commands] == expected_central
            assert len({command.command for command in commands}) == 3
    assert commands_for("buyer", "en")[1].description == "🧾 My subscriptions"
    assert commands_for("owner", "ru")[1].description == "🧾 Мои подписки"
    assert commands_for("buyer", "ru")[2].description == "🛟 Поддержка"
    assert all(
        not command.command.startswith("/")
        for role in ("buyer", "owner", "central", "platform")
        for command in commands_for(role, "en")
    )


async def test_native_menu_default_and_language_specific_chat_override():
    bot = _bot()
    await install_public_commands(bot, customer=True)
    assert bot.set_my_commands.await_count == 2
    first, second = bot.set_my_commands.await_args_list
    assert first.kwargs["scope"].type == "all_private_chats"
    assert [c.command for c in first.kwargs["commands"]] == [
        "menu", "subscription", "support",
    ]
    assert first.kwargs["commands"][0].description == "🏠 Main menu"
    assert second.kwargs["language_code"] == "ru"
    assert second.kwargs["commands"][0].description == "🏠 Главное меню"

    await set_chat_commands(bot, chat_id=711, role="owner", language="en")
    command = bot.set_my_commands.await_args
    assert command.kwargs["scope"].type == "chat"
    assert command.kwargs["scope"].chat_id == 711
    assert [c.command for c in command.kwargs["commands"]] == [
        "menu", "subscription", "support",
    ]
    assert command.kwargs["commands"][1].description == "🧾 My subscriptions"


async def test_central_platform_owner_override_uses_own_chat_only(monkeypatch):
    from telegram_membership_saas import telegram_menus

    bot = _bot()
    monkeypatch.setattr(
        telegram_menus, "get_settings",
        lambda: SimpleNamespace(platform_owner_telegram_id=777),
    )
    updated = AsyncMock()
    monkeypatch.setattr(telegram_menus, "set_chat_commands", updated)
    await refresh_user_commands(
        bot, MagicMock(), chat_id=777, language="en",
    )
    assert updated.await_args.kwargs["role"] == "platform"
    assert updated.await_args.kwargs["language"] == "en"
    await refresh_user_commands(
        bot, MagicMock(), chat_id=778, language="ru",
    )
    assert updated.await_args.kwargs["role"] == "central"
    assert updated.await_args.kwargs["language"] == "ru"


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
        assert "contact ViMi Pay support" in text
        assert not any("@" in label for label, _ in _buttons(keyboard))
        assert any(button.url == SUPPORT_URL
                   for row in keyboard.inline_keyboard for button in row)
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
        receipt_label = next(
            label for label, cb in _buttons(keyboard) if cb.startswith("paid:")
        )
        assert request_id.hex[:10] in receipt_label
        assert "Premium" in receipt_label
        assert request_id.hex[:10] in text
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
        assert 'format="dt">12.12.2026 13:00</tg-time>' in text
        assert f'unix="{int(ended.timestamp())}"' in text
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


async def test_cancel_consumed_step_revokes_inflight_generation(monkeypatch):
    from telegram_membership_saas.modules.owner_setup import service

    state = SimpleNamespace(
        step=None, payload={}, generation="still-valid",
        prompt_message_id=None, selected_project_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=state))

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(service, "tenant_session_scope", scope)
    changed = await service.cancel_owner_flow(
        MagicMock(), actor_id=uuid4(), bot_scope_id=9000,
    )
    assert changed is True
    assert state.generation is None
    assert state.expires_at <= datetime.now(UTC)
    assert state.selected_project_id is not None


def test_checkout_has_return_navigation_and_no_implicit_cancel():
    request_id = uuid4()
    keyboard = commerce._checkout_keyboard(request_id)
    labels = _buttons(keyboard)
    assert ("🧾 Мои покупки", "checkoutnav:home") not in labels
    assert ("💎 К тарифам", "checkoutnav:catalog") in labels
    assert len(labels) == 3
    assert labels[1][0] == "Отменить заявку"


def test_multiple_receipt_buttons_identify_matching_requests():
    first, second = uuid4(), uuid4()
    account = BuyerAccount(
        requests_count=3,
        requests=(
            BuyerRequest(first, "Premium", "waiting_confirmation",
                         Decimal("550"), "RUB"),
            BuyerRequest(uuid4(), "Premium", "waiting_review",
                         Decimal("550"), "RUB"),
            BuyerRequest(second, "Premium", "waiting_confirmation",
                         Decimal("550"), "RUB"),
        ),
    )
    language = _language.set("en")
    try:
        text, keyboard = account_list_screen(account, section="reqs", page=0)
        labels = [(label, cb) for label, cb in _buttons(keyboard)
                  if cb.startswith("paid:")]
        assert len(labels) == 2
        assert first.hex[:10] in labels[0][0]
        assert second.hex[:10] in labels[1][0]
        assert labels[0][1] != labels[1][1]
        assert first.hex[:10] in text and second.hex[:10] in text
        assert "Under review" in text
        assert all(len(label) <= 64 for label, _ in labels)
    finally:
        _language.reset(language)



async def test_checkout_navigation_preserves_original_payment_message(monkeypatch):
    """Entering account or catalog never overwrites payment instructions."""
    @asynccontextmanager
    async def read_scope(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(commerce, "system_project_session_scope", read_scope)
    reader = AsyncMock(return_value=BuyerAccount())
    monkeypatch.setattr(commerce, "load_buyer_account", reader)
    catalog = AsyncMock(return_value=True)
    monkeypatch.setattr(commerce, "_send_catalog", catalog)

    bot = _bot()
    query = SimpleNamespace(
        data="checkoutnav:home",
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=200), message_id=777,
            text="Checkout: 550 RUB\nPay to ...",
        ),
        from_user=SimpleNamespace(id=200),
        answer=AsyncMock(),
    )
    router = commerce.create_telegram_commerce_router(None, project_id=uuid4())
    handler = next(
        x.callback for x in router.callback_query.handlers
        if x.callback.__name__ == "checkout_navigation_callback"
    )
    await handler(query, bot)
    assert "Мои покупки" in bot.send_message.await_args.kwargs["text"]
    assert bot.edit_message_text.await_count == 0
    assert reader.await_count == 1
    query.answer.assert_awaited_once()

    query.data = "checkoutnav:catalog"
    await handler(query, bot)
    assert catalog.await_count == 1
    assert "message_id" not in catalog.await_args.kwargs
    assert bot.edit_message_text.await_count == 0

    query.message.chat.id = 999  # forged/forwarded callback
    reader.reset_mock()
    catalog.reset_mock()
    await handler(query, bot)
    assert query.answer.await_args.kwargs["show_alert"] is True
    reader.assert_not_awaited()
    catalog.assert_not_awaited()


async def test_buyer_account_commands_keep_owner_menu_when_authorized(monkeypatch):
    @asynccontextmanager
    async def read_scope(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(commerce, "system_project_session_scope", read_scope)
    monkeypatch.setattr(commerce, "load_buyer_account",
                        AsyncMock(return_value=BuyerAccount()))
    refresh = AsyncMock()
    monkeypatch.setattr(commerce, "refresh_user_commands", refresh)
    bot = _bot()
    router = commerce.create_telegram_commerce_router(
        MagicMock(), project_id=uuid4(),
    )
    commands = {item.callback.__name__: item.callback
                for item in router.message.handlers}
    owner = SimpleNamespace(
        from_user=SimpleNamespace(id=100),
        chat=SimpleNamespace(type="private", id=100),
    )
    token = _language.set("en")
    try:
        await commands["subscriptions_command"](owner, bot)
        refresh.assert_awaited_once()
        assert refresh.await_args.kwargs["project_id"] is not None
        assert refresh.await_args.kwargs["language"] == "en"
        refresh.reset_mock()
        await commands["support_command"](owner, bot)
        refresh.assert_awaited_once()
        refresh.reset_mock()
        await commands["buyer_exit_command"](owner, bot)
        refresh.assert_awaited_once()
    finally:
        _language.reset(token)
