"""Lava.top real-email checkout routing without real provider I/O or payments."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import CallbackQuery

from telegram_membership_saas.config import Settings
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_saas_lava_client as client
from telegram_membership_saas.modules import platform_saas_lava_handlers as lava
from telegram_membership_saas.modules.platform_saas_gateway_service import GatewayLink
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


@pytest.fixture(autouse=True)
def clear_lava_reply_cache():
    lava._pending_lava.clear()
    yield
    lava._pending_lava.clear()


def _settings(**overrides):
    return Settings(
        _env_file=None, telegram_central_access_enabled=False,
        saas_lava_top_api_key="TEST_PLATFORM_API_KEY",
        saas_lava_top_offer_id=uuid4(),
        **overrides,
    )


def _handler(router, name):
    return next(h.callback for h in router.message.handlers
                if h.callback.__name__ == name)


def _message(payer, *, text, reply=None, chat_type="private"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=payer, type=chat_type),
        from_user=SimpleNamespace(id=payer),
        text=text, reply_to_message=reply,
    )


@pytest.mark.parametrize("language", ("ru", "en"))
async def test_deeplink_collects_actual_email_and_issues_one_link(
    monkeypatch, language,
):
    payer, invoice, bot_id = 11901, uuid4(), 912
    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    balance = AsyncMock(return_value={"remaining_rub": "150.00"})
    monkeypatch.setattr(lava, "saas_v2_invoice_balance", balance)
    issued = AsyncMock(return_value=GatewayLink(
        "lava", uuid4(), "https://app.lava.top/pay/real-link",
        Decimal("150.00"),
    ))
    monkeypatch.setattr(lava, "issue_platform_lava_checkout", issued)
    bot = MagicMock()
    bot.id = bot_id
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 500
    router = lava.create_platform_saas_lava_router(MagicMock())
    locale = _language.set(language)
    try:
        start = _message(payer, text="/start sl_" + encode_uuid(invoice))
        await _handler(router, "open_lava_from_original_bot")(start, bot)
        assert 1 == bot.send_message.await_count
        assert (payer, 500) in lava._pending_lava
        assert balance.await_count == 1
        prompt = bot.send_message.await_args
        buttons = prompt.kwargs["reply_markup"].inline_keyboard
        assert buttons[0][0].callback_data == "lava:cancel"
        assert "/cancel" not in prompt.args[1]
        assert ("обычным сообщением" if language == "ru" else "normal message") in prompt.args[1]
        incoming = _message(payer, text="buyer+pay@example.com")
        assert await lava._LavaEmailReplyFilter()(incoming, bot)
        await _handler(router, "accept_lava_email_reply")(incoming, bot)
        issued.assert_awaited_once()
        assert issued.await_args.kwargs["customer_email"] == "buyer+pay@example.com"
        assert issued.await_args.kwargs["invoice_id"] == invoice
        assert issued.await_args.kwargs["central_bot_id"] == bot_id
        assert bot.send_message.await_args.kwargs[
            "reply_markup"
        ].inline_keyboard[0][0].url == "https://app.lava.top/pay/real-link"
        assert not await lava._LavaEmailReplyFilter()(incoming, bot)
        assert len(lava._pending_lava) == 0
    finally:
        _language.reset(locale)


async def test_other_payer_and_nonprivate_chat_never_receive_invoice(monkeypatch):
    issue = AsyncMock()
    monkeypatch.setattr(lava, "issue_platform_lava_checkout", issue)
    router = lava.create_platform_saas_lava_router(MagicMock())
    bot = MagicMock()
    bot.id = 100
    bot.send_message = AsyncMock()
    start = _message(123, text="/start sl_" + encode_uuid(uuid4()),
                     chat_type="group")
    await _handler(router, "open_lava_from_original_bot")(start, bot)
    assert len(lava._pending_lava) == 0
    assert not await lava._LavaEmailReplyFilter()(
        _message(456, text="test@example.com", reply=SimpleNamespace(
            message_id=777, from_user=SimpleNamespace(id=100),
        )), bot,
    )
    issue.assert_not_awaited()


async def test_prompt_fails_closed_without_credentials_before_touching_database(
    monkeypatch,
):
    monkeypatch.setattr(lava, "get_settings", lambda: Settings(_env_file=None))
    ensure = AsyncMock()
    monkeypatch.setattr(lava, "ensure_telegram_actor", ensure)
    bot = MagicMock()
    bot.id = 100
    bot.send_message = AsyncMock()
    with pytest.raises(client.LavaTopError):
        await lava.send_platform_lava_email_prompt(
            bot, MagicMock(), payer_telegram_id=123, invoice_id=uuid4(),
        )
    ensure.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("balance", ["0.00", "49.99", "1000000.01", "NaN"])
async def test_unpayable_original_invoice_rejected_before_email(
    monkeypatch, balance,
):
    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": balance}),
    )
    bot = MagicMock()
    bot.id = 100
    bot.send_message = AsyncMock()
    with pytest.raises(client.LavaTopError):
        await lava.send_platform_lava_email_prompt(
            bot, MagicMock(), payer_telegram_id=123, invoice_id=uuid4(),
        )
    bot.send_message.assert_not_awaited()


async def test_invalid_email_prompts_again_without_reserving_or_charging(
    monkeypatch,
):
    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    issue = AsyncMock()
    monkeypatch.setattr(lava, "issue_platform_lava_checkout", issue)
    bot = MagicMock()
    bot.id = 100
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 501
    invoice = uuid4()
    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=123, invoice_id=invoice,
    )
    router = lava.create_platform_saas_lava_router(MagicMock())
    incoming = _message(
        123, text="bad@localhost", reply=SimpleNamespace(
            message_id=501, from_user=SimpleNamespace(id=100),
        ),
    )
    await _handler(router, "accept_lava_email_reply")(incoming, bot)
    issue.assert_not_awaited()
    assert (123, 501) in lava._pending_lava
    assert bot.send_message.await_count == 2


async def test_expired_reply_does_not_create_a_lava_invoice(monkeypatch):
    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    issue = AsyncMock()
    monkeypatch.setattr(lava, "issue_platform_lava_checkout", issue)
    now = [12345.0]
    monkeypatch.setattr(lava, "monotonic", lambda: now[0])
    bot = MagicMock()
    bot.id = 100
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 502
    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=123, invoice_id=uuid4(),
    )
    now[0] += lava.EMAIL_REPLY_SECONDS + 1
    router = lava.create_platform_saas_lava_router(MagicMock())
    incoming = _message(
        123, text="test@example.com", reply=SimpleNamespace(
            message_id=502, from_user=SimpleNamespace(id=100),
        ),
    )
    await _handler(router, "accept_lava_email_reply")(incoming, bot)
    assert len(lava._pending_lava) == 0
    issue.assert_not_awaited()
    assert "истёк" in bot.send_message.await_args.args[1] or (
        "expired" in bot.send_message.await_args.args[1]
    )


@pytest.mark.parametrize("email", [
    None, "", "no-at-sign", "abc@localhost", "a..b@example.com",
    " user@example.com", "name@-example.com",
])
def test_email_validation_never_sends_bad_input_to_provider(email):
    with pytest.raises(ValueError):
        client.validate_lava_customer_email(email)
    assert client.validate_lava_customer_email(
        "merchant+test@example.com"
    ) == "merchant+test@example.com"


@pytest.mark.parametrize("code", [
    "LAVA-HTTP-400-CURRENCY",
    "LAVA-HTTP-401",
    "LAVA-HTTP-404",
    "LAVA-RESERVE",
    "LAVA-BIND",
])
async def test_checkout_failure_reports_only_safe_code_and_clears_email_prompt(
    monkeypatch, code,
):
    from telegram_membership_saas.modules.platform_saas_lava_client import LavaTopError

    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    invoice = uuid4()
    bot = MagicMock()
    bot.id = 9001
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 902
    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=123, invoice_id=invoice,
    )
    issue = AsyncMock(side_effect=LavaTopError(
        "sensitive provider error email=PRIVATE_EMAIL TOKEN_PRIVATE",
        code=code,
    ))
    monkeypatch.setattr(lava, "issue_platform_lava_checkout", issue)
    msg = _message(123, text="user@example.com")
    router = lava.create_platform_saas_lava_router(MagicMock())
    assert await lava._LavaEmailReplyFilter()(msg, bot)
    await _handler(router, "accept_lava_email_reply")(msg, bot)
    issue.assert_awaited_once()
    assert len(lava._pending_lava) == 0
    out = bot.send_message.await_args
    assert code in out.args[1]
    assert "PRIVATE_EMAIL" not in out.args[1]
    assert "TOKEN_PRIVATE" not in out.args[1]
    assert out.kwargs["reply_markup"].__class__.__name__ == "ReplyKeyboardRemove"


async def test_cancel_lava_email_leaves_original_invoice_untouched(monkeypatch):
    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    issue = AsyncMock()
    monkeypatch.setattr(lava, "issue_platform_lava_checkout", issue)
    bot = MagicMock()
    bot.id = 9000
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 907
    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=123, invoice_id=uuid4(),
    )
    router = lava.create_platform_saas_lava_router(MagicMock())
    cancel = _message(123, text="/cancel")
    assert await lava._LavaCancelFilter()(cancel, bot)
    await _handler(router, "cancel_lava_email")(cancel, bot)
    assert lava._pending_lava == {}
    assert not await lava._LavaEmailReplyFilter()(
        _message(123, text="user@example.com"), bot,
    )
    issue.assert_not_awaited()
    out = bot.send_message.await_args
    assert "отменён" in out.args[1]
    assert out.kwargs["reply_markup"].__class__.__name__ == "ReplyKeyboardRemove"


async def test_rich_cancel_only_disarms_own_current_lava_prompt():
    bot = MagicMock()
    bot.id = 900
    bot.send_message = AsyncMock()
    router = lava.create_platform_saas_lava_router(MagicMock())
    handler = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "cancel_lava_email_button"
    )
    invoice = uuid4()
    lava._pending_lava[(123, 500)] = lava._PendingEmail(
        invoice, bot.id, lava.monotonic() + 600,
    )

    def callback(message_id):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(
                message_id=message_id,
                chat=SimpleNamespace(id=123, type="private"),
            ),
            data="lava:cancel", answer=AsyncMock(),
        )

    old = callback(499)
    await handler(old, bot)
    assert (123, 500) in lava._pending_lava
    assert old.answer.await_args.kwargs["show_alert"] is True
    bot.send_message.assert_not_awaited()

    current = callback(500)
    await handler(current, bot)
    assert lava._pending_lava == {}
    assert "отменён" in bot.send_message.await_args.args[1]
    keyboard = bot.send_message.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "own:saasinvoices:0"
    current.answer.assert_awaited_once()


async def test_lava_cancel_callback_is_not_cleared_before_handler():
    invoice = uuid4()
    lava._pending_lava[(123, 500)] = lava._PendingEmail(
        invoice, 900, lava.monotonic() + 600,
    )
    navigation = lava.ClearPendingLavaOnNavigation()
    callback = AsyncMock()
    def event(data):
        return CallbackQuery.model_construct(
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=123, type="private"),
            ),
            data=data,
        )
    await navigation(callback, event("lava:cancel"), {})
    assert (123, 500) in lava._pending_lava
    await navigation(callback, event("own:promptback:old"), {})
    assert (123, 500) in lava._pending_lava
    await navigation(callback, event("own:promptcancel:old"), {})
    assert (123, 500) in lava._pending_lava
    await navigation(callback, event("own:managedcancel:old"), {})
    assert (123, 500) in lava._pending_lava
    await navigation(callback, event("own:projects"), {})
    assert lava._pending_lava == {}


async def test_lava_cleanup_after_owner_navigation_is_scoped():
    invoice = uuid4()
    lava._pending_lava[(123, 500)] = lava._PendingEmail(
        invoice, 900, lava.monotonic() + 600,
    )
    lava._pending_lava[(456, 501)] = lava._PendingEmail(
        uuid4(), 900, lava.monotonic() + 600,
    )
    lava.clear_pending_lava_after_owner_navigation(
        payer_id=123, central_bot_id=901,
    )
    assert len(lava._pending_lava) == 2
    lava.clear_pending_lava_after_owner_navigation(
        payer_id=123, central_bot_id=900,
    )
    assert (123, 500) not in lava._pending_lava
    assert (456, 501) in lava._pending_lava


async def test_non_lava_reply_and_unrelated_user_not_swallowed(monkeypatch):
    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    bot = MagicMock()
    bot.id = 9000
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 907
    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=123, invoice_id=uuid4(),
    )
    checker = lava._LavaEmailReplyFilter()
    assert not await checker(_message(321, text="user@example.com"), bot)
    assert not await checker(_message(123, text="/menu"), bot)
    assert not await checker(
        _message(123, text="user@example.com", chat_type="group"), bot,
    )
    assert not await checker(
        _message(123, text="user@example.com", reply=SimpleNamespace(
            message_id=300, from_user=SimpleNamespace(id=9000),
        )), bot,
    )
    assert not await checker(
        _message(123, text="user@example.com", reply=SimpleNamespace(
            message_id=907, from_user=SimpleNamespace(id=77),
        )), bot,
    )
    assert await checker(
        _message(123, text="user@example.com", reply=SimpleNamespace(
            message_id=907, from_user=SimpleNamespace(id=9000),
        )), bot,
    )


async def test_lava_pending_never_intercepts_botfather_credential(monkeypatch):
    from telegram_membership_saas.modules.owner_setup.handlers import (
        StrayBotTokenFilter,
    )

    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    bot = MagicMock()
    bot.id = 904
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 908
    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=123, invoice_id=uuid4(),
    )
    token_message = _message(
        123, text="123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    )
    assert not await lava._LavaEmailReplyFilter()(token_message, bot)
    # The existing central security handler must still scrub tokens.
    assert await StrayBotTokenFilter()(token_message)


async def test_unrelated_central_navigation_disarms_email_capture(monkeypatch):
    from datetime import UTC, datetime

    from aiogram.types import CallbackQuery, Chat, Message, User

    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    bot = MagicMock()
    bot.id = 900
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 903
    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=123, invoice_id=uuid4(),
    )
    middleware = lava.ClearPendingLavaOnNavigation()
    seen = []

    async def downstream(event, data):
        seen.append(event)
        return "continued"

    me = User(id=123, is_bot=False, first_name="Payer")
    stranger = User(id=456, is_bot=False, first_name="Other")
    original = Message(
        message_id=1, date=datetime.now(UTC),
        chat=Chat(id=123, type="private"), from_user=me,
        text="/menu",
    )
    other = Message(
        message_id=2, date=datetime.now(UTC),
        chat=Chat(id=456, type="private"), from_user=stranger,
        text="/menu",
    )
    assert await middleware(downstream, other, {}) == "continued"
    assert lava._pending_lava
    assert await middleware(downstream, original, {}) == "continued"
    assert not lava._pending_lava
    assert len(seen) == 2

    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=123, invoice_id=uuid4(),
    )
    callback = CallbackQuery(
        id="callback-x", from_user=me, chat_instance="private-session",
        data="own:projects",
        message=original,
    )
    await middleware(downstream, callback, {})
    assert not lava._pending_lava
    assert not await lava._LavaEmailReplyFilter()(
        _message(123, text="payer@example.com"), bot,
    )


async def test_lava_cancel_command_stays_available_before_navigation_middleware(
    monkeypatch,
):
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, User

    monkeypatch.setattr(lava, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    bot = MagicMock()
    bot.id = 900
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 903
    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=123, invoice_id=uuid4(),
    )
    message = Message(
        message_id=8, date=datetime.now(UTC),
        chat=Chat(id=123, type="private"),
        from_user=User(id=123, is_bot=False, first_name="Payer"),
        text="/cancel",
    )
    await lava.ClearPendingLavaOnNavigation()(
        lambda *_: _async_true(), message, {},
    )
    assert lava._pending_lava


async def _async_true():
    return True

async def test_platform_lava_shortlink_opt_in_keeps_original_order(monkeypatch):
    from telegram_membership_saas.modules.platform_saas_payment_redirect import (
        short_lava_url,
    )

    payer, invoice, order = 33771, uuid4(), uuid4()
    settings = Settings(
        _env_file=None, telegram_central_access_enabled=False,
        saas_lava_top_api_key="TEST_KEY", saas_lava_top_offer_id=uuid4(),
        saas_shortlinks_enabled=True,
    )
    monkeypatch.setattr(lava, "get_settings", lambda: settings)
    monkeypatch.setattr(
        lava, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        lava, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    issue = AsyncMock(return_value=GatewayLink(
        "lava", order, "https://app.lava.top/pay/long?paymentParams=SENSITIVE",
        Decimal("150.00"),
    ))
    monkeypatch.setattr(lava, "issue_platform_lava_checkout", issue)
    bot = MagicMock()
    bot.id = 9009
    bot.send_message = AsyncMock()
    bot.send_message.return_value.message_id = 300
    await lava.send_platform_lava_email_prompt(
        bot, MagicMock(), payer_telegram_id=payer, invoice_id=invoice,
    )
    router = lava.create_platform_saas_lava_router(MagicMock())
    await _handler(router, "accept_lava_email_reply")(
        _message(payer, text="test@example.com"), bot,
    )
    assert issue.await_count == 1
    result = bot.send_message.await_args.kwargs["reply_markup"]
    assert result.inline_keyboard[0][0].url == short_lava_url(order)
    assert "Оплатить" in result.inline_keyboard[0][0].text
