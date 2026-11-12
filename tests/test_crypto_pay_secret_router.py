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
    # Same secret, pasted with spaces/newline while quoting an old buyer
    # notification, MUST still be captured and deleted before forwarding.
    assert await guard(_message("  123456:" + "C" * 35 + "\n"), bot) is True
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
        assert called == ["deleted", "read"]
        assert kwargs["api_token"] == "123456:" + "C" * 35
        called.append("connected")

    async def read(*_args, **kwargs):
        assert called == ["deleted"]
        called.append("read")
        return SetupFlow(
            "crypto_pay_token", project, {"environment": "mainnet"}, 15,
            generation="original-gen",
        )

    async def consume(*_args, **kwargs):
        assert called == ["deleted", "read", "connected"]
        assert kwargs["generation"] == "original-gen"
        assert kwargs["discard"] is True
        called.append("consumed")

    monkeypatch.setattr(
        crypto, "ensure_telegram_actor", AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(
        crypto, "read_crypto_pay_secret_prompt", AsyncMock(side_effect=read),
    )
    monkeypatch.setattr(
        crypto, "consume_button_flow", AsyncMock(side_effect=consume),
    )
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
    assert called == ["deleted", "read", "connected", "consumed"]
    assert "123456:" not in str(bot.send_message.await_args_list)


async def test_failed_secret_deletion_never_reaches_provider(monkeypatch):
    project = uuid4()
    consume, read, connect = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(crypto, "consume_button_flow", consume)
    monkeypatch.setattr(crypto, "read_crypto_pay_secret_prompt", read)
    monkeypatch.setattr(crypto, "connect_crypto_pay", connect)
    bot = MagicMock()
    bot.delete_message = AsyncMock(return_value=False)
    bot.send_message = AsyncMock()
    router = crypto.create_crypto_secret_router(MagicMock(), project_id=project)
    await _router_handler(router)(_message(), bot)
    read.assert_not_awaited()
    consume.assert_not_awaited()
    connect.assert_not_awaited()


@pytest.mark.parametrize(
    "payload", ["1", "ordinary message", "/cancel", "not even a token"],
)
def test_plain_messages_do_not_trigger_secret_db_filter(payload):
    assert not crypto._plausible_merchant_token(payload)


@pytest.mark.parametrize(
    "pasted", [
        "  123456:" + "C" * 35 + "\n",
        "\n123456:" + "C" * 35 + "   ",
        "\t123456:" + "C" * 35 + "\r\n",
    ],
)
async def test_quoted_padded_merchant_secret_is_deleted_before_connect(
    monkeypatch, pasted,
):
    project = uuid4()
    monkeypatch.setattr(
        crypto, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        crypto, "active_crypto_pay_secret_prompt", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        crypto, "read_crypto_pay_secret_prompt", AsyncMock(return_value=SetupFlow(
            "crypto_pay_token", project, {"environment": "mainnet"}, 15,
            generation="original-gen",
        )),
    )
    connected = AsyncMock()
    monkeypatch.setattr(crypto, "connect_crypto_pay", connected)
    monkeypatch.setattr(crypto, "consume_button_flow", AsyncMock())
    monkeypatch.setattr(
        crypto, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="example-key"),
    )
    guard = crypto.CryptoPaySecretFilter(MagicMock(), project)
    bot = MagicMock()
    bot.id = 654321
    bot.delete_message = AsyncMock(return_value=True)
    bot.send_message = AsyncMock()
    message = _message(pasted)
    assert await guard(message, bot)
    await _router_handler(
        crypto.create_crypto_secret_router(MagicMock(), project_id=project),
    )(message, bot)
    bot.delete_message.assert_awaited_once()
    assert connected.await_args.kwargs["api_token"] == pasted.strip()
    assert pasted.strip() not in str(bot.send_message.await_args_list)


async def test_provider_outage_keeps_secret_guard_active_for_retry(
    monkeypatch,
):
    """No token is forwarded after a failed getMe, even when quoting a buyer."""
    from telegram_membership_saas.modules.crypto_pay.client import CryptoPayError

    project, actor = uuid4(), uuid4()
    flow = SetupFlow(
        "crypto_pay_token", project, {"environment": "mainnet"}, 15,
        generation="original-gen",
    )
    # A real persistent owner state remains active through the provider call;
    # read() does not mutate it, and consume() must run ONLY on success.
    read = AsyncMock(return_value=flow)
    connect = AsyncMock(side_effect=[
        CryptoPayError("Crypto Pay API is temporarily unavailable"),
        None,
    ])
    consume = AsyncMock()
    monkeypatch.setattr(
        crypto, "ensure_telegram_actor", AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(
        crypto, "read_crypto_pay_secret_prompt", read,
    )
    monkeypatch.setattr(crypto, "connect_crypto_pay", connect)
    monkeypatch.setattr(crypto, "consume_button_flow", consume)
    monkeypatch.setattr(
        crypto, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="example-key"),
    )
    bot = MagicMock()
    bot.id = 654321
    bot.delete_message = AsyncMock(return_value=True)
    bot.send_message = AsyncMock()
    router = crypto.create_crypto_secret_router(
        MagicMock(), project_id=project,
    )
    handler = _router_handler(router)
    msg = _message()  # Still quotes an old buyer notification.
    await handler(msg, bot)
    consume.assert_not_awaited()
    assert read.await_count == 1
    assert any(
        label in str(bot.send_message.await_args)
        for label in ("Защищённый ввод остаётся активным", "Protected input remains active")
    )
    # On retry the same old buyer quote cannot intercept the token.
    await handler(msg, bot)
    assert connect.await_count == 2
    assert read.await_count == 2
    consume.assert_awaited_once_with(
        read.await_args.args[0], actor_id=actor,
        expected_step="crypto_pay_token",
        generation="original-gen", bot_scope_id=bot.id, discard=True,
    )
    assert "123456:" not in str(bot.send_message.await_args_list)


async def test_concurrent_owner_navigation_is_not_discarded_after_verification(
    monkeypatch,
):
    project = uuid4()
    monkeypatch.setattr(
        crypto, "ensure_telegram_actor", AsyncMock(return_value=uuid4()),
    )
    monkeypatch.setattr(
        crypto, "read_crypto_pay_secret_prompt",
        AsyncMock(return_value=SetupFlow(
            "crypto_pay_token", project, {"environment": "mainnet"},
            15, generation="old",
        )),
    )
    monkeypatch.setattr(crypto, "connect_crypto_pay", AsyncMock())
    monkeypatch.setattr(
        crypto, "consume_button_flow", AsyncMock(
            side_effect=ValueError("A newer owner form replaced this wizard"),
        ),
    )
    monkeypatch.setattr(
        crypto, "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key="example-key"),
    )
    bot = MagicMock()
    bot.id = 654321
    bot.delete_message = AsyncMock(return_value=True)
    bot.send_message = AsyncMock()
    await _router_handler(
        crypto.create_crypto_secret_router(MagicMock(), project_id=project),
    )(_message(), bot)
    assert "123456:" not in str(bot.send_message.await_args_list)
    assert any(
        label in str(bot.send_message.await_args)
        for label in ("защищённо подключён", "securely connected")
    )
