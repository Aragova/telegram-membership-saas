"""Central-bot provider-authenticated ViMi Pay Stars checkout handlers."""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_saas_stars_handlers as stars_ui
from telegram_membership_saas.modules import platform_saas_stars_service as service
from telegram_membership_saas.modules.owner_setup import handlers as owner_ui
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def handler(router, name, *, kind="message"):
    events = getattr(router, kind).handlers
    return next(h.callback for h in events if h.callback.__name__ == name)


@pytest.mark.parametrize("language", ("ru", "en"))
async def test_central_deep_link_sends_native_star_invoice(
    monkeypatch, language,
):
    invoice = uuid4()
    quote = {
        "order_id": str(uuid4()),
        "payload": "vps:" + str(uuid4()),
        "stars_amount": 60, "rub_amount": "150.00",
    }
    create = AsyncMock(return_value=quote)
    monkeypatch.setattr(stars_ui, "create_platform_stars_order", create)
    router = stars_ui.create_platform_saas_stars_router(MagicMock())
    bot = MagicMock()
    bot.id = 998877
    bot.send_invoice = AsyncMock()
    tg = 707070
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=tg),
        from_user=SimpleNamespace(id=tg),
        text="/start sa_" + encode_uuid(invoice),
    )
    locale = _language.set(language)
    try:
        await handler(router, "open_platform_stars_from_deeplink")(message, bot)
    finally:
        _language.reset(locale)
    assert create.await_args.kwargs["invoice_id"] == invoice
    assert create.await_args.kwargs["central_bot_id"] == 998877
    kwargs = bot.send_invoice.await_args.kwargs
    assert kwargs["currency"] == "XTR"
    assert kwargs["payload"] == quote["payload"]
    assert kwargs["prices"][0].amount == 60
    assert "Telegram Stars" in kwargs["prices"][0].label


@pytest.mark.parametrize("language", ("ru", "en"))
async def test_central_owner_invoice_pay_opens_native_stars(
    monkeypatch, language,
):
    invoice = uuid4()
    actor = uuid4()
    monkeypatch.setattr(
        owner_ui, "ensure_telegram_actor", AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(
        owner_ui, "saas_v2_invoice_balance",
        AsyncMock(return_value={"remaining_rub": "150.00"}),
    )
    stars_send = AsyncMock()
    monkeypatch.setattr(stars_ui, "send_platform_stars_invoice", stars_send)
    router = owner_ui.create_owner_setup_router(
        MagicMock(), fixed_project_id=None,
    )
    bot = MagicMock()
    bot.id = 123
    bot.send_message = AsyncMock()
    tg = 737373
    query = SimpleNamespace(
        data="own:spr:" + encode_uuid(invoice) + ":S:0",
        from_user=SimpleNamespace(id=tg, username="owner", first_name="Owner"),
        message=SimpleNamespace(chat=SimpleNamespace(type="private", id=tg)),
        answer=AsyncMock(),
    )
    locale = _language.set(language)
    try:
        await handler(router, "owner_callback", kind="callback_query")(query, bot)
    finally:
        _language.reset(locale)
    stars_send.assert_awaited_once()
    assert stars_send.await_args.kwargs["invoice_id"] == invoice
    assert stars_send.await_args.kwargs["payer_telegram_id"] == tg
    bot.send_message.assert_not_awaited()
    query.answer.assert_awaited_once()


async def test_precheckout_wrong_currency_is_refused_before_db(monkeypatch):
    rpc = AsyncMock()
    monkeypatch.setattr(service, "_rpc", rpc)
    assert not await service.platform_stars_precheckout(
        MagicMock(), payer_telegram_id=111,
        central_bot_id=222, payload="vps:some-order",
        currency="RUB", stars_amount=50,
    )
    rpc.assert_not_awaited()


async def test_central_provider_precheckout_denied_safely(monkeypatch):
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(stars_ui, "platform_stars_precheckout", check)
    bot = MagicMock()
    bot.id = 12345
    bot.answer_pre_checkout_query = AsyncMock()
    query = SimpleNamespace(
        id="query-123", from_user=SimpleNamespace(id=123),
        invoice_payload="vps:" + str(uuid4()),
        currency="XTR", total_amount=60,
    )
    router = stars_ui.create_platform_saas_stars_router(MagicMock())
    await handler(
        router, "platform_stars_pre_checkout", kind="pre_checkout_query",
    )(query, bot)
    kwargs = bot.answer_pre_checkout_query.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["error_message"]


@pytest.mark.parametrize("outcome,notices", [
    ({"applied": True, "duplicate": False}, 1),
    ({"applied": True, "duplicate": True}, 0),
    ({"applied": False, "duplicate": False}, 1),
])
async def test_signed_telegram_charge_handler_outcome(
    monkeypatch, outcome, notices,
):
    confirm = AsyncMock(return_value=outcome)
    monkeypatch.setattr(stars_ui, "confirm_platform_stars_charge", confirm)
    router = stars_ui.create_platform_saas_stars_router(MagicMock())
    bot = MagicMock()
    bot.id = 12345
    bot.send_message = AsyncMock()
    tg = 737373
    stamp = datetime.now(UTC)
    pay = SimpleNamespace(
        invoice_payload="vps:" + str(uuid4()),
        currency="XTR", total_amount=60,
        telegram_payment_charge_id="charge123456789",
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=tg),
        from_user=SimpleNamespace(id=tg),
        successful_payment=pay, date=stamp,
    )
    await handler(router, "platform_stars_success")(message, bot)
    assert confirm.await_args.kwargs["payer_telegram_id"] == tg
    assert confirm.await_args.kwargs["central_bot_id"] == 12345
    assert confirm.await_args.kwargs["charge_id"] == pay.telegram_payment_charge_id
    assert bot.send_message.await_count == notices


async def test_verified_refund_records_one_immutable_event(monkeypatch):
    refund_record = AsyncMock(return_value=True)
    monkeypatch.setattr(
        stars_ui, "record_platform_stars_refund", refund_record,
    )
    router = stars_ui.create_platform_saas_stars_router(MagicMock())
    bot = MagicMock()
    bot.id = 12345
    bot.send_message = AsyncMock()
    tg = 737373
    refund = SimpleNamespace(
        invoice_payload="vps:" + str(uuid4()),
        currency="XTR", total_amount=60,
        telegram_payment_charge_id="charge123456789",
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=tg),
        from_user=SimpleNamespace(id=tg),
        refunded_payment=refund, date=datetime.now(UTC),
    )
    await handler(router, "platform_stars_refunded")(message, bot)
    refund_record.assert_awaited_once()
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("language", ("ru", "en"))
async def test_owner_only_rail_readiness_is_configuration_not_charge(
    monkeypatch, language,
):
    owner = 737373
    settings = Settings(
        _env_file=None, telegram_central_access_enabled=False,
        platform_owner_telegram_id=owner,
        saas_lava_top_api_key="SECRET_LAVA_DO_NOT_ECHO",
        saas_lava_top_offer_id=uuid4(),
        saas_crypto_pay_api_token="SECRET_CRYPTO_DO_NOT_ECHO",
        saas_crypto_pay_testnet=True,
        environment="staging",
    )
    monkeypatch.setattr(stars_ui, "get_settings", lambda: settings)
    rate = AsyncMock(return_value=Decimal("2.50"))
    monkeypatch.setattr(stars_ui, "platform_stars_rate", rate)
    router = stars_ui.create_platform_saas_stars_router(MagicMock())
    bot = MagicMock()
    bot.send_message = AsyncMock()
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=owner),
        from_user=SimpleNamespace(id=owner),
        text="/saasrails",
    )
    locale = _language.set(language)
    try:
        await handler(router, "platform_rails_readiness")(message, bot)
        rate.assert_awaited_once()
        bot.send_message.assert_awaited_once()
        content = bot.send_message.await_args.args[1]
        assert "Lava.top" in content
        assert "Telegram Stars" in content
        assert "Crypto Pay" in content
        assert "2.50 ₽ / ⭐" in content
        assert ("тестовая сеть" if language == "ru" else "testnet") in content
        assert ("проверка настроек" if language == "ru" else "Configuration only") in content
        assert "SECRET_LAVA" not in content
        assert "SECRET_CRYPTO" not in content
        assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
        assert "reply_markup" not in bot.send_message.await_args.kwargs
    finally:
        _language.reset(locale)


@pytest.mark.parametrize("actor,chat_type,central_access", [
    (999999, "private", False),
    (737373, "group", False),
    (737373, "private", True),
])
async def test_rail_readiness_never_reveals_settings_outside_central_owner(
    monkeypatch, actor, chat_type, central_access,
):
    settings = Settings(
        _env_file=None, telegram_central_access_enabled=central_access,
        platform_owner_telegram_id=737373,
        saas_lava_top_api_key="PRIVATE_DO_NOT_ECHO",
    )
    monkeypatch.setattr(stars_ui, "get_settings", lambda: settings)
    rate = AsyncMock()
    monkeypatch.setattr(stars_ui, "platform_stars_rate", rate)
    router = stars_ui.create_platform_saas_stars_router(MagicMock())
    bot = MagicMock()
    bot.send_message = AsyncMock()
    message = SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=actor),
        from_user=SimpleNamespace(id=actor),
        text="/saasrails",
    )
    await handler(router, "platform_rails_readiness")(message, bot)
    rate.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_rail_readiness_fails_closed_on_unknown_stars_rate_and_testnet(
    monkeypatch,
):
    settings = Settings(
        _env_file=None, telegram_central_access_enabled=False,
        platform_owner_telegram_id=737373,
        environment="production", saas_crypto_pay_testnet=True,
        saas_crypto_pay_api_token="SECRET_TESTNET_DO_NOT_ECHO",
    )
    monkeypatch.setattr(stars_ui, "get_settings", lambda: settings)
    monkeypatch.setattr(
        stars_ui, "platform_stars_rate",
        AsyncMock(side_effect=RuntimeError("private DB failure")),
    )
    router = stars_ui.create_platform_saas_stars_router(MagicMock())
    bot = MagicMock()
    bot.send_message = AsyncMock()
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=737373),
        from_user=SimpleNamespace(id=737373),
        text="/saasrails",
    )
    locale = _language.set("ru")
    try:
        await handler(router, "platform_rails_readiness")(message, bot)
    finally:
        _language.reset(locale)
    text = bot.send_message.await_args.args[1]
    assert "Проверка ставки недоступна" in text
    assert "Нет подходящего API-токена" in text
    assert "private DB failure" not in text
    assert "SECRET_TESTNET" not in text
