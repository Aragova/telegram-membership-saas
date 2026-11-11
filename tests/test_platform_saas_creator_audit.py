"""Creator audit checks real Telegram roles, without ever writing exemptions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_saas_creator_check as audit
from telegram_membership_saas.modules.platform_dashboard_handlers import (
    _platform_creator_check_text,
)
from telegram_membership_saas.rich_messages import to_rich_html


@pytest.mark.parametrize(
    ("owner_role", "bot_role", "expected"),
    [
        ("creator", "administrator", "creator"),
        ("administrator", "administrator", "admin_only"),
        ("member", "administrator", "not_creator"),
        ("creator", "member", "no_access"),
    ],
)
async def test_telegram_creator_only_is_not_merely_admin(
    monkeypatch, owner_role, bot_role, expected,
):
    remote = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=555)),
        get_chat=AsyncMock(return_value=SimpleNamespace(id=-100123, type="channel")),
        get_chat_member=AsyncMock(side_effect=[
            SimpleNamespace(status=bot_role),
            SimpleNamespace(status=owner_role),
        ]),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(audit, "Bot", lambda token: remote)
    result = await audit._telegram_creator_status(
        "not-a-real-token", expected_bot_id=555, channel_chat_id=-100123,
        owner_telegram_id=123,
    )
    assert result == expected
    remote.session.close.assert_awaited_once()
    if bot_role != "administrator":
        assert remote.get_chat_member.await_count == 1


async def test_bot_id_mismatch_fails_closed_and_makes_no_channel_calls(monkeypatch):
    remote = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=999)),
        get_chat=AsyncMock(),
        get_chat_member=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(audit, "Bot", lambda token: remote)
    result = await audit._telegram_creator_status(
        "private-token", expected_bot_id=555, channel_chat_id=-100123,
        owner_telegram_id=123,
    )
    assert result == "unavailable"
    remote.get_chat.assert_not_awaited()
    remote.get_chat_member.assert_not_awaited()
    remote.session.close.assert_awaited_once()


async def test_telegram_errors_do_not_leak_token_or_exempt_anything(monkeypatch):
    remote = SimpleNamespace(
        get_me=AsyncMock(
            side_effect=ValueError("SECRET TOKEN IN URL"),
        ),
        get_chat=AsyncMock(),
        get_chat_member=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(audit, "Bot", lambda token: remote)
    result = await audit._telegram_creator_status(
        "secret-token", expected_bot_id=555, channel_chat_id=-100123,
        owner_telegram_id=123,
    )
    assert result == "unavailable"
    assert "SECRET" not in result
    remote.session.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("status", "expected_text"),
    [
        ("creator", "создатель"),
        ("admin_only", "только администратор"),
        ("unavailable", "Telegram недоступен"),
    ],
)
def test_live_telegram_view_shows_ephemeral_proof_not_exemptions(
    status, expected_text,
):
    snapshot = audit.CreatorAuditResult(
        page=0, pages=4,
        rows=(audit.CreatorAuditRow("my_channel_bot", status),),
    )
    body = _platform_creator_check_text(snapshot)
    rich = to_rich_html(body)
    assert expected_text in rich
    assert "<table bordered compact>" in rich
    assert "Никакие исключения и счета не изменены" in rich
    assert snapshot.creators == (1 if status == "creator" else 0)


@pytest.mark.asyncio
async def test_missing_or_ambiguous_channels_never_trigger_telegram_calls(
    monkeypatch,
):
    actor = uuid4()
    bot_id, project_id = uuid4(), uuid4()
    instance = SimpleNamespace(
        id=bot_id, project_id=project_id, username="demo_bot",
        telegram_bot_id=555, token_ciphertext="secret-ciphertext",
    )
    project = SimpleNamespace(id=project_id, owner_user_id=actor)
    awaiter = AsyncMock(return_value={
        "page": 0, "pages": 1, "pending_bots": [
            {"username": "demo_bot", "state": "not_exempt"},
        ],
    })
    monkeypatch.setattr(audit, "platform_saas_exemption_page", awaiter)
    remote = AsyncMock()
    monkeypatch.setattr(audit, "_telegram_creator_status", remote)

    class FakeSession:
        async def execute(self, statement):
            return SimpleNamespace(all=lambda: [(instance, project)])
        async def scalars(self, statement):
            return SimpleNamespace(all=lambda: [])

    class FakeScope:
        async def __aenter__(self):
            return FakeSession()
        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(audit, "tenant_session_scope", lambda *a: FakeScope())
    result = await audit.audit_creator_page(
        AsyncMock(), actor_user_id=actor,
        actor_telegram_id=7777, page=0,
        settings=__import__(
            "telegram_membership_saas.config", fromlist=["Settings"]
        ).Settings(platform_owner_telegram_id=7777),
    )
    assert result.rows[0].status == "channel_ambiguous"
    remote.assert_not_awaited()


def test_live_check_english_rich_screen_preserves_status_distinctions():
    token = _language.set("en")
    try:
        data = audit.CreatorAuditResult(
            page=0, pages=1,
            rows=(
                audit.CreatorAuditRow("owner_bot", "creator"),
                audit.CreatorAuditRow("admin_bot", "admin_only"),
            ),
        )
        formatted = to_rich_html(_platform_creator_check_text(data))
    finally:
        _language.reset(token)
    assert "<table bordered compact>" in formatted
    assert "✅ creator" in formatted
    assert "administrator only" in formatted
    assert "No exemption records or invoices changed" in formatted
    assert data.creators == 1
