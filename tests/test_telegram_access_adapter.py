from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.access import AccessActionType, AccessCommand
from telegram_membership_saas.modules.telegram_access import (
    TelegramAccessAdapter,
    TelegramAccessCommand,
    project_id_from_invite_name,
)


class FakeBot:
    def __init__(self, *, status: str = "left", is_member: bool | None = None) -> None:
        self.status = status
        self.is_member = is_member
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get_chat(self, **kwargs):
        self.calls.append(("get_chat", kwargs))
        return SimpleNamespace(id=-1009876543210)

    async def get_chat_member(self, **kwargs):
        self.calls.append(("get_chat_member", kwargs))
        return SimpleNamespace(status=self.status, is_member=self.is_member)

    async def unban_chat_member(self, **kwargs):
        self.calls.append(("unban_chat_member", kwargs))
        return True

    async def create_chat_invite_link(self, **kwargs):
        self.calls.append(("create_chat_invite_link", kwargs))
        return SimpleNamespace(invite_link="https://t.me/+secure-test")

    async def send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return True

    async def revoke_chat_invite_link(self, **kwargs):
        self.calls.append(("revoke_chat_invite_link", kwargs))
        return True

    async def ban_chat_member(self, **kwargs):
        self.calls.append(("ban_chat_member", kwargs))
        return True


def _command(action_type: AccessActionType) -> TelegramAccessCommand:
    project_id = uuid4()
    return TelegramAccessCommand(
        access=AccessCommand(
            action_id=uuid4(),
            project_id=project_id,
            customer_id=uuid4(),
            resource_id=uuid4(),
            action_type=action_type,
            claim_attempt=1,
        ),
        telegram_user_id=123456789,
        chat_id=-1001234567890,
    )


async def test_grant_creates_short_join_request_link_for_exact_project() -> None:
    now = datetime(2026, 11, 3, 14, 0, tzinfo=UTC)
    bot = FakeBot()
    adapter = TelegramAccessAdapter(
        bot,  # type: ignore[arg-type]
        invite_ttl=timedelta(minutes=15),
        clock=lambda: now,
    )
    command = _command(AccessActionType.GRANT)

    result = await adapter.prepare(command)
    await adapter.deliver(command, result)

    assert result.invite_link == "https://t.me/+secure-test"
    assert result.invite_expires_at == now + timedelta(minutes=15)
    assert result.already_member is False
    assert result.resolved_chat_id == -1009876543210

    invite_call = next(kwargs for name, kwargs in bot.calls if name == "create_chat_invite_link")
    assert invite_call["name"] == command.access.project_id.hex
    assert invite_call["creates_join_request"] is True
    assert invite_call["expire_date"] == result.invite_expires_at

    message_call = next(kwargs for name, kwargs in bot.calls if name == "send_message")
    assert message_call["chat_id"] == command.telegram_user_id
    assert "Доступ к каналу готов" in str(message_call["text"])
    assert result.invite_link not in str(message_call["text"])
    assert message_call["parse_mode"] == "HTML"
    button = message_call["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Вступить в канал"
    assert button.url == result.invite_link
    assert button.style == "success"


async def test_grant_does_not_create_invite_for_existing_member() -> None:
    bot = FakeBot(status="member")
    adapter = TelegramAccessAdapter(bot)  # type: ignore[arg-type]

    result = await adapter.prepare(_command(AccessActionType.GRANT))

    assert result.already_member is True
    assert result.invite_link is None
    assert all(name != "create_chat_invite_link" for name, _ in bot.calls)


async def test_restricted_non_member_still_gets_join_request_invite() -> None:
    bot = FakeBot(status="restricted", is_member=False)
    adapter = TelegramAccessAdapter(bot)  # type: ignore[arg-type]

    result = await adapter.prepare(_command(AccessActionType.GRANT))

    assert result.already_member is False
    assert result.invite_link == "https://t.me/+secure-test"


async def test_restricted_current_member_skips_invite() -> None:
    bot = FakeBot(status="restricted", is_member=True)
    adapter = TelegramAccessAdapter(bot)  # type: ignore[arg-type]

    result = await adapter.prepare(_command(AccessActionType.GRANT))

    assert result.already_member is True
    assert result.invite_link is None


async def test_revoke_bans_member_from_resource() -> None:
    bot = FakeBot()
    adapter = TelegramAccessAdapter(bot)  # type: ignore[arg-type]
    command = _command(AccessActionType.REVOKE)

    await adapter.prepare(command)

    ban_call = next(kwargs for name, kwargs in bot.calls if name == "ban_chat_member")
    assert ban_call["chat_id"] == command.chat_id
    assert ban_call["user_id"] == command.telegram_user_id
    assert ban_call["revoke_messages"] is False


async def test_username_resource_is_resolved_to_numeric_chat_id() -> None:
    bot = FakeBot()
    adapter = TelegramAccessAdapter(bot)  # type: ignore[arg-type]
    command = _command(AccessActionType.GRANT)
    command = TelegramAccessCommand(
        access=command.access,
        telegram_user_id=command.telegram_user_id,
        chat_id="@private_channel",
    )

    result = await adapter.prepare(command)

    assert result.resolved_chat_id == -1009876543210
    invite_call = next(kwargs for name, kwargs in bot.calls if name == "create_chat_invite_link")
    assert invite_call["chat_id"] == -1009876543210


@pytest.mark.parametrize("value", [None, "", "not-a-project", "0" * 31, "g" * 32])
def test_project_id_from_invite_name_rejects_untrusted_names(value: str | None) -> None:
    assert project_id_from_invite_name(value) is None


def test_project_id_from_invite_name_accepts_exact_uuid_hex() -> None:
    project_id = uuid4()
    assert project_id_from_invite_name(project_id.hex) == project_id
