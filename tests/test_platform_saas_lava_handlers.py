"""Lava.top real-email checkout routing without real provider I/O or payments."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

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
        assert prompt.kwargs["reply_markup"].__class__.__name__ == "ReplyKeyboardRemove"
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
