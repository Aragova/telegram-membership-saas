from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots import service as bots
from telegram_membership_saas.modules.telegram_admin import handlers as admin

TEST_TOKEN = "123456:" + "A" * 35


def _key() -> SecretStr:
    return SecretStr(Fernet.generate_key().decode("ascii"))


def _bot():
    bot = MagicMock()
    bot.id = 987
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock(return_value=True)
    return bot


def _router():
    return admin.create_telegram_admin_router(None, project_id=uuid4())


def _handler(name, *, callback=False):
    router = _router()
    observer = router.callback_query if callback else router.message
    return next(h.callback for h in observer.handlers if h.callback.__name__ == name)


def _token_message(*, token=TEST_TOKEN, chat_type="private", prompt_bot_id=987):
    prompt = SimpleNamespace(
        text=admin.CONNECT_PROMPT_PREFIX + str(uuid4()),
        from_user=SimpleNamespace(id=prompt_bot_id),
    )
    return SimpleNamespace(
        message_id=55,
        chat=SimpleNamespace(type=chat_type, id=123),
        from_user=SimpleNamespace(id=123),
        reply_to_message=prompt,
        text=token,
    )


def test_registered_bot_model_does_not_store_plaintext_token():
    fields = BotInstance.__table__.columns.keys()
    assert "token_ciphertext" in fields
    assert "token" not in fields
    assert "telegram_bot_id" in fields


def test_fernet_roundtrip_and_wrong_key_rejected():
    key = _key()
    encrypted = bots.encrypt_token(TEST_TOKEN, key=key)
    assert TEST_TOKEN not in encrypted
    assert bots.decrypt_token(encrypted, key=key) == TEST_TOKEN
    with pytest.raises(InvalidToken):
        bots.decrypt_token(encrypted, key=_key())


def test_blank_encryption_key_means_not_configured():
    assert Settings(bot_token_encryption_key="").bot_token_encryption_key is None


def test_encryption_requires_configured_key():
    with pytest.raises(RuntimeError, match="Шифрование"):
        bots.encrypt_token(TEST_TOKEN, key=None)


async def test_verify_get_me_and_always_close_session(monkeypatch):
    candidate = MagicMock()
    candidate.get_me = AsyncMock(
        return_value=SimpleNamespace(
            id=2222, username="MyTestBot", first_name="Test Bot", is_bot=True
        )
    )
    candidate.session.close = AsyncMock()
    factory = MagicMock(return_value=candidate)
    monkeypatch.setattr(bots, "Bot", factory)

    result = await bots.verify_telegram_bot(TEST_TOKEN)

    assert result.telegram_bot_id == 2222
    assert result.username == "MyTestBot"
    candidate.get_me.assert_awaited_once()
    candidate.session.close.assert_awaited_once()


async def test_telegram_transport_exception_cannot_leak_token(monkeypatch):
    candidate = MagicMock()
    candidate.get_me = AsyncMock(
        side_effect=RuntimeError(f"Telegram URL /bot{TEST_TOKEN}/getMe")
    )
    candidate.session.close = AsyncMock()
    monkeypatch.setattr(bots, "Bot", MagicMock(return_value=candidate))
    with pytest.raises(ValueError, match="Telegram не подтвердил токен") as error:
        await bots.verify_telegram_bot(TEST_TOKEN)
    assert TEST_TOKEN not in str(error.value)
    assert error.value.__cause__ is None
    candidate.session.close.assert_awaited_once()


async def test_invalid_token_does_not_make_telegram_call(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(bots, "Bot", factory)
    with pytest.raises(ValueError, match="Неверный формат"):
        await bots.verify_telegram_bot("not-a-token")
    factory.assert_not_called()


async def test_unauthorized_actor_does_not_verify_token(monkeypatch):
    auth = AsyncMock(side_effect=PermissionError("no"))
    verify = AsyncMock()
    monkeypatch.setattr(bots, "require_bot_owner", auth)
    monkeypatch.setattr(bots, "verify_telegram_bot", verify)
    with pytest.raises(PermissionError):
        await bots.register_customer_bot(
            None,
            project_id=uuid4(),
            actor_telegram_user_id=42,
            token=TEST_TOKEN,
            encryption_key=_key(),
            central_bot_id=987,
        )
    verify.assert_not_awaited()


async def test_central_bot_cannot_be_registered(monkeypatch):
    monkeypatch.setattr(bots, "require_bot_owner", AsyncMock())
    monkeypatch.setattr(
        bots,
        "verify_telegram_bot",
        AsyncMock(return_value=bots.VerifiedBot(987, "ViMiPayBot", "ViMi")),
    )
    with pytest.raises(ValueError, match="управляющего"):
        await bots.register_customer_bot(
            None, project_id=uuid4(), actor_telegram_user_id=1,
            token=TEST_TOKEN, encryption_key=_key(), central_bot_id=987,
        )


async def test_registration_persists_only_ciphertext(monkeypatch):
    monkeypatch.setattr(bots, "require_bot_owner", AsyncMock())
    monkeypatch.setattr(bots, "_actor_user_id", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(bots, "_require_owner", AsyncMock())
    monkeypatch.setattr(
        bots, "verify_telegram_bot",
        AsyncMock(return_value=bots.VerifiedBot(2222, "BuyerBot", "Buyer Bot")),
    )
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    created = []

    def add(obj):
        created.append(obj)

    session.add = add

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(bots, "tenant_session_scope", scope)
    result = await bots.register_customer_bot(
        None, project_id=uuid4(), actor_telegram_user_id=1,
        token=TEST_TOKEN, encryption_key=_key(), central_bot_id=987,
    )
    assert result.username == "BuyerBot"
    assert result.updated is False
    assert len(created) == 1
    assert created[0].telegram_bot_id == 2222
    assert created[0].status == "registered"
    assert TEST_TOKEN not in created[0].token_ciphertext
    session.flush.assert_awaited_once()


async def test_token_reply_deleted_before_registration(monkeypatch):
    register = AsyncMock(return_value=bots.BotRegistration(2222, "OwnerBot", False))
    monkeypatch.setattr(admin, "register_customer_bot", register)
    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key=_key()),
    )
    bot = _bot()
    msg = _token_message()
    handler = _handler("connect_bot_token_handler")
    await handler(msg, bot)

    bot.delete_message.assert_awaited_once_with(chat_id=123, message_id=55)
    register.assert_awaited_once()
    assert register.await_args.kwargs["token"] == TEST_TOKEN
    outgoing = bot.send_message.await_args.kwargs["text"]
    assert TEST_TOKEN not in outgoing
    assert "@OwnerBot" in outgoing


async def test_delete_failure_refuses_to_register(monkeypatch):
    register = AsyncMock()
    monkeypatch.setattr(admin, "register_customer_bot", register)
    bot = _bot()
    bot.delete_message.return_value = False

    await _handler("connect_bot_token_handler")(_token_message(), bot)

    register.assert_not_awaited()
    assert "отменено" in bot.send_message.await_args.kwargs["text"]


async def test_connect_button_requires_owner_and_key(monkeypatch):
    owner = AsyncMock()
    monkeypatch.setattr(admin, "require_bot_owner", owner)
    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: SimpleNamespace(bot_token_encryption_key=_key()),
    )
    bot = _bot()
    callback = SimpleNamespace(
        data="adm:connect",
        from_user=SimpleNamespace(id=123),
        message=SimpleNamespace(
            chat=SimpleNamespace(type="private", id=123), message_id=99
        ),
        answer=AsyncMock(),
    )
    await _handler("admin_navigation_handler", callback=True)(callback, bot)
    owner.assert_awaited_once()
    assert admin.CONNECT_PROMPT_PREFIX in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["reply_markup"].selective is True


async def test_wrong_user_reply_is_not_accepted():
    bot = _bot()
    flt = admin.BotTokenReplyFilter()
    assert await flt(_token_message(), bot) is True
    assert await flt(_token_message(prompt_bot_id=234), bot) is False
    assert await flt(_token_message(chat_type="supergroup"), bot) is False


async def test_stray_token_is_deleted_not_registered(monkeypatch):
    register = AsyncMock()
    monkeypatch.setattr(admin, "register_customer_bot", register)
    bot = _bot()
    msg = _token_message()
    msg.reply_to_message = None

    await _handler("stray_bot_token_handler")(msg, bot)

    bot.delete_message.assert_awaited_once_with(chat_id=123, message_id=55)
    register.assert_not_awaited()
    assert TEST_TOKEN not in bot.send_message.await_args.kwargs["text"]
