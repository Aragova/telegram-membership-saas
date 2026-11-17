"""Central-bot-only Crypto Pay routing and bounded provider reconciliation."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules import platform_saas_gateway_handlers as ui
from telegram_membership_saas.modules import platform_saas_gateway_recovery as recovery
from telegram_membership_saas.modules import platform_saas_gateway_service as service
from telegram_membership_saas.modules.platform_saas_gateway_service import GatewayLink
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


def _settings(**kwargs):
    return Settings(
        _env_file=None, telegram_central_access_enabled=False,
        platform_owner_telegram_id=7878, **kwargs,
    )


def _handler(router, name):
    return next(item.callback for item in router.message.handlers
                if item.callback.__name__ == name)


async def test_original_tenant_invoice_opens_real_central_crypto_deeplink(
    monkeypatch,
):
    invoice_id = uuid4()
    payer = 101010
    link = GatewayLink("crypto", uuid4(),
                       "https://t.me/CryptoBot?start=IV123",
                       Decimal("150"))
    issue = AsyncMock(return_value=link)
    monkeypatch.setattr(ui, "issue_platform_crypto_checkout", issue)
    monkeypatch.setattr(ui, "get_settings", lambda: _settings())
    router = ui.create_platform_saas_gateway_router(MagicMock())
    bot = MagicMock()
    bot.id = 99999
    bot.send_message = AsyncMock()
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=payer),
        from_user=SimpleNamespace(id=payer),
        text="/start sc_" + encode_uuid(invoice_id),
    )
    await _handler(router, "open_crypto_from_original_bot")(message, bot)
    assert issue.await_args.kwargs["invoice_id"] == invoice_id
    assert issue.await_args.kwargs["payer_telegram_id"] == payer
    assert issue.await_args.kwargs["central_bot_id"] == 99999
    sent = bot.send_message.await_args
    assert sent.args[0] == payer
    assert sent.kwargs["reply_markup"].inline_keyboard[0][0].url == link.invoice_url


async def test_invalid_platform_deeplink_never_creates_provider_invoice(monkeypatch):
    issue = AsyncMock()
    monkeypatch.setattr(ui, "issue_platform_crypto_checkout", issue)
    router = ui.create_platform_saas_gateway_router(MagicMock())
    bot = MagicMock()
    bot.send_message = AsyncMock()
    message = SimpleNamespace(
        chat=SimpleNamespace(type="group", id=2020),
        from_user=SimpleNamespace(id=101),
        text="/start sc_" + encode_uuid(uuid4()),
    )
    await _handler(router, "open_crypto_from_original_bot")(message, bot)
    issue.assert_not_awaited()


async def test_gateway_refuses_tenant_settings_without_any_provider_calls(
    monkeypatch,
):
    reserve = AsyncMock()
    monkeypatch.setattr(service, "_reserve", reserve)
    with pytest.raises(PermissionError):
        await service.issue_platform_crypto_checkout(
            MagicMock(), settings=Settings(
                _env_file=None, telegram_central_access_enabled=True,
                saas_crypto_pay_api_token="TEST_PLATFORM_ONLY",
            ), payer_telegram_id=1313, central_bot_id=555,
            invoice_id=uuid4(),
        )
    reserve.assert_not_awaited()


async def test_gateway_scanner_leaves_unpaid_provider_rows_for_retry(monkeypatch):
    order = {
        "id": str(uuid4()), "provider": "crypto",
        "payer_telegram_id": 1010,
    }
    pending = AsyncMock(return_value=(order,))
    verify = AsyncMock(return_value=None)
    monkeypatch.setattr(recovery, "list_platform_pending_gateway_orders", pending)
    monkeypatch.setattr(recovery, "verify_and_record_platform_gateway_payment", verify)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    count = await recovery.recover_platform_gateway_once(
        MagicMock(), bot=bot,
        settings=_settings(saas_crypto_pay_api_token="TEST_PLATFORM_ONLY"),
    )
    assert count == 0
    verify.assert_awaited_once()
    bot.send_message.assert_not_awaited()


async def test_gateway_scanner_applies_verified_once(monkeypatch):
    from telegram_membership_saas import localization

    # Existing RU notification test uses the same saved-language lookup
    # as production rather than a dummy MagicMock database connection.
    monkeypatch.setattr(
        localization, "read_language", AsyncMock(return_value="ru"),
    )
    order = {
        "id": str(uuid4()), "provider": "crypto",
        "payer_telegram_id": 1010,
    }
    pending = AsyncMock(return_value=(order,))
    verify = AsyncMock(return_value={
        "applied": True, "duplicate": False,
    })
    monkeypatch.setattr(recovery, "list_platform_pending_gateway_orders", pending)
    monkeypatch.setattr(recovery, "verify_and_record_platform_gateway_payment", verify)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    count = await recovery.recover_platform_gateway_once(
        MagicMock(), bot=bot,
        settings=_settings(saas_crypto_pay_api_token="TEST_PLATFORM_ONLY"),
    )
    assert count == 1
    bot.send_message.assert_awaited_once()


async def test_missing_platform_credentials_never_selects_orders(monkeypatch):
    pending = AsyncMock()
    monkeypatch.setattr(recovery, "list_platform_pending_gateway_orders", pending)
    assert await recovery.recover_platform_gateway_once(
        MagicMock(), bot=MagicMock(), settings=_settings(),
    ) == 0
    pending.assert_not_awaited()



async def test_private_operator_can_record_verified_refund_without_new_menu(
    monkeypatch,
):
    record = AsyncMock(return_value={
        "recorded": True,
        "amount_rub": "150.00",
        "credit_reversed_rub": "0.00",
    })
    monkeypatch.setattr(ui, "record_operator_verified_gateway_refund", record)
    monkeypatch.setattr(ui, "get_settings", lambda: _settings())
    router = ui.create_platform_saas_gateway_router(MagicMock())
    bot = MagicMock()
    bot.send_message = AsyncMock()
    msg = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=7878),
        from_user=SimpleNamespace(id=7878),
        text="/saasrefund crypto 4815162342 refund-confirmed-123 150 VERIFIED",
    )
    await _handler(router, "platform_verified_provider_refund")(msg, bot)
    record.assert_awaited_once()
    assert record.await_args.kwargs["provider"] == "crypto"
    assert record.await_args.kwargs["provider_invoice_id"] == "4815162342"
    assert record.await_args.kwargs["provider_refund_id"] == "refund-confirmed-123"
    assert record.await_args.kwargs["amount_rub"] == Decimal("150")
    text = bot.send_message.await_args.args[1]
    assert "150.00" in text
    assert "0.00" in text


async def test_verified_refund_rejects_non_operator_or_missing_confirmation(
    monkeypatch,
):
    record = AsyncMock()
    monkeypatch.setattr(ui, "record_operator_verified_gateway_refund", record)
    monkeypatch.setattr(ui, "get_settings", lambda: _settings())
    router = ui.create_platform_saas_gateway_router(MagicMock())
    callback = _handler(router, "platform_verified_provider_refund")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    cmd = "/saasrefund crypto 4815162342 refund-confirmed-123 150 VERIFIED"
    forged = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=9009),
        from_user=SimpleNamespace(id=9009), text=cmd,
    )
    await callback(forged, bot)
    record.assert_not_awaited()
    bot.send_message.assert_not_awaited()

    no_proof = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=7878),
        from_user=SimpleNamespace(id=7878),
        text="/saasrefund crypto 4815162342 refund-confirmed-123 150",
    )
    await callback(no_proof, bot)
    record.assert_not_awaited()
    assert bot.send_message.await_count == 1



async def test_gateway_scanner_notifies_each_payer_in_saved_english(
    monkeypatch,
):
    from telegram_membership_saas import localization

    order = {
        "id": str(uuid4()), "provider": "lava",
        "payer_telegram_id": 1010,
    }
    monkeypatch.setattr(
        recovery, "list_platform_pending_gateway_orders",
        AsyncMock(return_value=(order,)),
    )
    monkeypatch.setattr(
        recovery, "verify_and_record_platform_gateway_payment",
        AsyncMock(return_value={"applied": True, "duplicate": False}),
    )
    # recipient_language must consult the saved preference rather than the
    # background worker's default RU context.
    lang = AsyncMock(return_value="en")
    monkeypatch.setattr(localization, "read_language", lang)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    count = await recovery.recover_platform_gateway_once(
        MagicMock(), bot=bot,
        settings=_settings(
            saas_lava_top_api_key="TEST_PLATFORM_API_KEY",
            saas_lava_top_offer_id=uuid4(),
        ),
    )
    assert count == 1
    lang.assert_awaited_once()
    assert lang.await_args.args[1] == 1010
    msg = bot.send_message.await_args.args[1]
    assert "invoice paid" in msg and "provider confirmed" in msg
