"""Backup monitor: independent GCS manifest, alerts, existing UI and permissions."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_backup_monitor as monitor

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def test_completed_manifest_only_and_reject_malformed_gcs_objects():
    good = {
        "name": "vimi-pay/postgres/20261116T103509Z-2160e558-0366-4b30-bd89-f69fe2c53c45/"
                "manifest.sha256",
        "timeCreated": "2026-11-16T10:35:30Z",
    }
    assert monitor._manifest_created(good, now=NOW) == datetime(
        2026, 11, 16, 10, 35, 30, tzinfo=UTC,
    )
    assert monitor._manifest_created(
        {
                        **good,
                        "name": good["name"].replace(
                            "manifest.sha256", "membership.dump.age",
                        ),
                    },
        now=NOW,
    ) is None
    assert monitor._manifest_created({**good, "timeCreated": "bad"}, now=NOW) is None
    assert monitor._manifest_created(
        {**good, "timeCreated": "2027-01-01T00:00:00Z"}, now=NOW
    ) is None


def test_stale_failure_and_verification_failure_are_distinct():
    receipt = SimpleNamespace(result="failed", reported_at=NOW - timedelta(hours=1))
    recent = NOW - timedelta(hours=3)
    assert monitor._condition(recent, verified=True, receipt=receipt, now=NOW) == "failed"
    assert monitor._condition(
        NOW - timedelta(minutes=30), verified=True, receipt=receipt, now=NOW,
    ) == "healthy"
    assert monitor._condition(
        recent, verified=False, receipt=None, now=NOW,
    ) == "verify_error"
    assert monitor._condition(
        NOW - timedelta(hours=26, minutes=1),
        verified=True, receipt=None, now=NOW,
    ) == "stale"
    assert monitor._condition(None, verified=True, receipt=None, now=NOW) == "stale"


@pytest.mark.parametrize("lang,title", [
    ("ru", "Резервная копия"),
    ("en", "Backup"),
])
def test_rich_existing_monitor_copy_and_notification_use_one_original_button(lang, title):
    token = _language.set(lang)
    try:
        body = monitor.backup_monitor_lines(
            {"state": "failed", "last_complete_at": NOW, "checked_at": NOW},
            timezone="Europe/Moscow",
        )
        assert title in body[0]
        assert len(body) == 2
        assert "<tg-time" in body[1]
        text = monitor._message(
            "problem", "failed", last=NOW, timezone="Europe/Moscow",
        )
        assert "ViMi Pay" in text
        assert "<tg-time" in text
    finally:
        _language.reset(token)


async def test_gcs_uses_read_only_metadata_token_and_complete_markers(monkeypatch):
    calls = []
    good = {
        "name": "vimi-pay/postgres/20261116T103509Z-2160e558-0366-4b30-bd89-f69fe2c53c45/"
                "manifest.sha256",
        "timeCreated": "2026-11-16T10:35:30Z",
    }

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            request = httpx.Request("GET", url)
            if len(calls) == 1:
                return httpx.Response(200, request=request, json={"access_token": "secret"})
            return httpx.Response(
                200, request=request, json={"items": [
                    good,
                    {**good, "name": good["name"].replace("manifest.sha256", "membership.dump.age")},
                ]},
            )
    monkeypatch.setattr(monitor.httpx, "AsyncClient", FakeClient)
    result = await monitor.latest_gcs_manifest("vimi-pay-backups-633224371599", now=NOW)
    assert result == datetime(2026, 11, 16, 10, 35, 30, tzinfo=UTC)
    assert calls[0][0] == monitor.METADATA_TOKEN_URL
    assert calls[1][1]["headers"]["Authorization"] == "Bearer secret"
    assert calls[1][1]["params"]["prefix"] == monitor.PREFIX


async def test_gcs_auth_errors_fail_closed_without_false_stale(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return httpx.Response(403, request=httpx.Request("GET", url))
    monkeypatch.setattr(monitor.httpx, "AsyncClient", FakeClient)
    with pytest.raises(monitor.BackupProbeError):
        await monitor.latest_gcs_manifest("valid-bucket", now=NOW)


async def test_backup_notice_reuses_existing_monitoring_button(monkeypatch):
    item = (monitor.uuid4(), 1, "problem", "stale", NOW)
    monkeypatch.setattr(monitor, "_claim", AsyncMock(return_value=item))
    finish = AsyncMock()
    monkeypatch.setattr(monitor, "_finish", finish)

    class LangContext:
        async def __aenter__(self):
            pass

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(monitor, "recipient_language", lambda *args: LangContext())
    bot = MagicMock()
    bot.send_message = AsyncMock()
    assert await monitor.deliver_one_backup_notice(
        MagicMock(), bot=bot, actor_id=monitor.uuid4(),
        owner_telegram_id=777, timezone="Europe/Moscow", now=NOW,
    )
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert len(markup.inline_keyboard) == 1
    assert len(markup.inline_keyboard[0]) == 1
    assert markup.inline_keyboard[0][0].callback_data == "plat:bots:30:0"
    assert finish.await_args.kwargs["delivered"] is True
