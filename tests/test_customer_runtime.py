"""Contract tests for project-scoped customer bot startup and routing."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from telegram_membership_saas.config import Settings
from telegram_membership_saas.customer_runtime import (
    check_customer_bot_identity,
    load_customer_bot,
    verify_resource_permissions,
)
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import encrypt_token
from telegram_membership_saas.modules.telegram_access import handlers as access
from telegram_membership_saas.telegram_adapter import (
    create_customer_dispatcher,
    create_dispatcher,
)

TEST_TOKEN = "123456:" + "A" * 35


def _settings(key=None):
    return Settings(
        bot_token_encryption_key=key,
        customer_telegram_bot_id=123456,
        telegram_project_id=uuid4(),
        _env_file=None,
    )


def _sf(monkeypatch, *, project_status="active", bots=()):
    from telegram_membership_saas import customer_runtime as runtime

    session = MagicMock()
    session.scalar = AsyncMock(return_value=project_status)
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: list(bots)))

    @asynccontextmanager
    async def scope(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(runtime, "system_project_session_scope", scope)
    return session


def test_customer_dispatcher_does_not_expose_admin_routes():
    dispatcher = create_customer_dispatcher(MagicMock(), project_id=uuid4())
    assert [r.name for r in dispatcher.sub_routers] == [
        "telegram-access", "telegram-commerce"
    ]


def test_central_can_drop_buyer_routes_for_cutover():
    dispatcher = create_dispatcher(MagicMock(), project_id=uuid4(), enable_access=False)
    assert [r.name for r in dispatcher.sub_routers] == [
        "owner-setup", "telegram-admin", "central-buyer-redirect"
    ]


def test_central_default_preserves_old_staging_path():
    dispatcher = create_dispatcher(MagicMock(), project_id=uuid4())
    assert [r.name for r in dispatcher.sub_routers] == [
        "owner-setup", "telegram-access", "telegram-admin", "telegram-commerce"
    ]


async def test_unauthorized_join_request_never_reaches_other_project(monkeypatch):
    scope = AsyncMock()
    monkeypatch.setattr(access, "system_project_session_scope", scope)
    target = uuid4()
    other = uuid4()
    bot = MagicMock()
    bot.decline_chat_join_request = AsyncMock()
    request = SimpleNamespace(
        chat=SimpleNamespace(id=-100999),
        from_user=SimpleNamespace(id=72),
        invite_link=SimpleNamespace(
            name=other.hex, invite_link="https://t.me/+other"
        ),
    )
    accepted = await access.process_chat_join_request(
        None, bot, request, expected_project_id=target
    )
    assert accepted is False
    bot.decline_chat_join_request.assert_awaited_once()
    scope.assert_not_awaited()


async def test_cannot_load_customer_bot_without_encryption_key(monkeypatch):
    _sf(monkeypatch)
    with pytest.raises(RuntimeError, match="encryption key"):
        await load_customer_bot(
            None, project_id=uuid4(), telegram_bot_id=123456, settings=_settings()
        )


async def test_customer_bot_fails_closed_when_project_is_suspended(monkeypatch):
    _sf(monkeypatch, project_status="suspended")
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    with pytest.raises(RuntimeError, match="project is not active"):
        await load_customer_bot(
            None, project_id=uuid4(), telegram_bot_id=123456,
            settings=_settings(key),
        )


async def test_customer_bot_loads_only_own_encrypted_registration(monkeypatch):
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    record = BotInstance(
        project_id=uuid4(),
        telegram_bot_id=123456,
        username="BrandedBuyerBot",
        display_name="Branded Buyer",
        token_ciphertext=encrypt_token(TEST_TOKEN, key=key),
        status="registered",
    )
    _sf(monkeypatch, bots=[record])
    result = await load_customer_bot(
        None, project_id=record.project_id, telegram_bot_id=123456,
        settings=_settings(key),
    )
    assert result.token == TEST_TOKEN
    assert result.project_id == record.project_id
    assert result.telegram_bot_id == 123456


@pytest.mark.parametrize("ids", [[], [123456, 999999], [999999]])
async def test_customer_bot_does_not_guess_between_bot_instances(monkeypatch, ids):
    key = SecretStr(Fernet.generate_key().decode("ascii"))
    rows = [
        BotInstance(
            project_id=uuid4(), telegram_bot_id=i,
            username=f"bot_{i}", display_name="Test",
            token_ciphertext=encrypt_token(TEST_TOKEN, key=key),
            status="registered",
        )
        for i in ids
    ]
    _sf(monkeypatch, bots=rows)
    with pytest.raises(RuntimeError, match="exactly one"):
        await load_customer_bot(
            None, project_id=uuid4(), telegram_bot_id=123456,
            settings=_settings(key),
        )


async def test_mismatched_telegram_get_me_denies_start_without_secret_leak():
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(id=999, is_bot=True))
    bot.get_webhook_info = AsyncMock()
    with pytest.raises(RuntimeError, match="identity") as exc:
        await check_customer_bot_identity(bot, 123456)
    assert exc.value.__cause__ is None
    bot.get_webhook_info.assert_not_awaited()


async def test_existing_webhook_fails_closed_without_modifying_it():
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(id=123456, is_bot=True))
    bot.get_webhook_info = AsyncMock(
        return_value=SimpleNamespace(url="https://example.invalid/another-service")
    )
    bot.delete_webhook = AsyncMock()
    with pytest.raises(RuntimeError, match="webhook"):
        await check_customer_bot_identity(bot, 123456)
    bot.delete_webhook.assert_not_awaited()


async def test_valid_telegram_identity_without_webhook_passes():
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(id=123456, is_bot=True))
    bot.get_webhook_info = AsyncMock(return_value=SimpleNamespace(url=""))
    await check_customer_bot_identity(bot, 123456)


async def test_resource_admin_rights_required_before_customer_sales(monkeypatch):
    _sf(monkeypatch, bots=[SimpleNamespace(external_id="-1001")])
    bot = MagicMock()
    bot.id = 123456
    bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(
            status="administrator", can_invite_users=True, can_restrict_members=False
        )
    )
    with pytest.raises(RuntimeError, match="administrator rights"):
        await verify_resource_permissions(None, project_id=uuid4(), bot=bot)


async def test_resource_permissions_pass_with_both_required_rights(monkeypatch):
    _sf(monkeypatch, bots=[SimpleNamespace(external_id="-1001")])
    bot = MagicMock()
    bot.id = 123456
    bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(
            status="administrator", can_invite_users=True, can_restrict_members=True
        )
    )
    await verify_resource_permissions(None, project_id=uuid4(), bot=bot)
    bot.get_chat_member.assert_awaited_once_with(chat_id="-1001", user_id=123456)


async def test_empty_resources_preserve_access_maintenance(monkeypatch):
    _sf(monkeypatch, bots=[])
    bot = MagicMock()
    bot.get_chat_member = AsyncMock()
    await verify_resource_permissions(None, project_id=uuid4(), bot=bot)
    bot.get_chat_member.assert_not_awaited()


def test_customer_bot_config_and_blank_id(monkeypatch):
    monkeypatch.setenv("TMS_CUSTOMER_TELEGRAM_BOT_ID", "")
    assert Settings(_env_file=None).customer_telegram_bot_id is None
    with pytest.raises(ValueError):
        Settings(customer_telegram_bot_id=-1, _env_file=None)


async def test_old_central_approval_buttons_redirect_without_changing_payment():
    dispatcher = create_dispatcher(MagicMock(), project_id=uuid4(), enable_access=False)
    router = dispatcher.sub_routers[-1]
    callback = next(
        h.callback for h in router.callback_query.handlers
        if h.callback.__name__ == "old_payment_decision_handler"
    )
    query = SimpleNamespace(answer=AsyncMock())
    await callback(query)
    assert query.answer.await_args.kwargs["show_alert"] is True
    assert "своего бота" in query.answer.await_args.args[0]


async def test_central_payment_command_directs_to_customer_bot():
    dispatcher = create_dispatcher(MagicMock(), project_id=uuid4(), enable_access=False)
    router = dispatcher.sub_routers[-1]
    handler = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "old_customer_command_handler"
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        chat=SimpleNamespace(type="private"),
    )
    await handler(message, bot)
    assert "подключённого бота" in bot.send_message.await_args.kwargs["text"]
    assert "/payments" in bot.send_message.await_args.kwargs["text"]
