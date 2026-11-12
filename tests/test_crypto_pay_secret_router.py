"""The merchant API token must never pass through older customer routes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.crypto_pay import owner_token_router as crypto
from telegram_membership_saas.modules.owner_setup.service import SetupFlow
from telegram_membership_saas.telegram_adapter import create_customer_dispatcher


def _message(text="123456:" + "C" * 35):
    return SimpleNamespace(
        message_id=77,
        text=text,
        from_user=SimpleNamespace(
            id=12345, username="seller", first_name="Owner",
        ),
        chat=SimpleNamespace(id=12345, type="private"),
        # An old reply target must NOT redirect the merchant token to buyer.
        reply_to_message=SimpleNamespace(
            message_id=999, text="🔔 Reply to buyer notification",
            from_user=SimpleNamespace(id=654321),
        ),
    )


def _router_handler(router):
    return next(
        x.callback for x in router.message.handlers
        if x.callback.__name__ == "capture_crypto_pay_token"
    )


def test_secret_router_precedes_all_composer_and_owner_routes():
    dispatcher = create_customer_dispatcher(MagicMock(), project_id=uuid4())
    names = [router.name for router in dispatcher.sub_routers]
    assert names.index("crypto-pay-secret-guard") < names.index("digital-file-sales")
    assert names.index("crypto-pay-secret-guard") < names.index("manual-file-checkout")
    assert names.index("crypto-pay-secret-guard") < names.index("owner-tenant")
    assert names.index("crypto-pay-secret-guard") < names.index("project-support")


async def test_secret_filter_only_intercepts_plausible_active_merchant_token(
    monkeypatch,
):
    project, actor = uuid4(), uuid4()
    monkeypatch.setattr(
        crypto, "ensure_telegram_actor", AsyncMock(return_value=actor),
    )
    active = AsyncMock(return_value=True)
    monkeypatch.setattr(crypto, "active_crypto_pay_secret_prompt", active)
    guard = crypto.CryptoPaySecretFilter(MagicMock(), project)
    bot = SimpleNamespace(id=654321)

    assert await guard(_message("ordinary buyer text"), bot) is False
    active.assert_not_awaited()

    assert await guard(_message(), bot) is True
    assert active.await_args.kwargs["project_id"] == project
    assert active.await_args.kwargs["bot_scope_id"] == bot.id
    active.return_value = False
    assert await guard(_message(), bot) is False


async def test_priority_handler_deletes_token_before_authentication_and_never_echoes(
    monkeypatch,
):
    project, actor = uuid4(), uuid4()
    called = []
    async def delete(*_args):
        called.append("deleted")
        return True

    async def connect(*_args, **kwargs):
        assert called == ["deleted", "consumed"]
        assert kwargs["api_token"] == "123456:" + "C" * 35
        called.append("connected")

    async def consume(*_args, **kwargs):
        assert called == ["deleted"]
        assert kwargs["reply_message_id"] is None
        called.append("consumed")
        return SetupFlow(
            "crypto_pay_token", project, {"environment": "mainnet"}, 15,
        )

    monkeypatch.setattr(
        crypto, "ensure_telegram_actor", AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(crypto, "consume_flow", AsyncMock(side_effect=consume))
    monkeypatch.setattr(crypto, "connect_crypto_pay", AsyncMock(side_effect=connect))
    monkeypatch.setattr(
        crypto, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="example-key"),
    )
    bot = MagicMock()
    bot.id = 654321
    bot.delete_message = AsyncMock(side_effect=delete)
    bot.send_message = AsyncMock()
    router = crypto.create_crypto_secret_router(MagicMock(), project_id=project)
    await _router_handler(router)(_message(), bot)
    assert called == ["deleted", "consumed", "connected"]
    assert "123456:" not in str(bot.send_message.await_args_list)


async def test_failed_secret_deletion_never_reaches_provider(monkeypatch):
    project = uuid4()
    consume, connect = AsyncMock(), AsyncMock()
    monkeypatch.setattr(crypto, "consume_flow", consume)
    monkeypatch.setattr(crypto, "connect_crypto_pay", connect)
    bot = MagicMock()
    bot.delete_message = AsyncMock(return_value=False)
    bot.send_message = AsyncMock()
    router = crypto.create_crypto_secret_router(MagicMock(), project_id=project)
    await _router_handler(router)(_message(), bot)
    consume.assert_not_awaited()
    connect.assert_not_awaited()


@pytest.mark.parametrize(
    "payload", ["1", "ordinary message", "/cancel", "not even a token"],
)
def test_plain_messages_do_not_trigger_secret_db_filter(payload):
    assert not crypto._plausible_merchant_token(payload)
