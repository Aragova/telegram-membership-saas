"""Backup monitor: independent GCS manifest, alerts, existing UI and permissions."""
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import (
    build_engine,
    build_session_factory,
    tenant_session_scope,
)
from telegram_membership_saas.localization import _language
from telegram_membership_saas.modules import platform_backup_monitor as monitor
from telegram_membership_saas.modules.owner_setup.service import ensure_telegram_actor
from telegram_membership_saas.tenant_context import TenantContext

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
                    {
                        **good,
                        "name": good["name"].replace(
                            "manifest.sha256", "membership.dump.age",
                        ),
                    },
                    {
                        **good,
                        "name": good["name"].replace(
                            "manifest.sha256", "pg-globals.sql.age",
                        ),
                    },
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



DB_AVAILABLE = bool(os.getenv("TMS_DATABASE_URL") and os.getenv("TMS_MIGRATION_DATABASE_URL"))


@pytest.mark.skipif(not DB_AVAILABLE, reason="Requires real PostgreSQL and RLS migrations")
async def test_real_postgres_failure_dedup_recovery_and_owner_only_scope(monkeypatch):
    """Two successive independent GCS checks: one problem and one recovery."""
    settings = Settings()
    engine = build_engine(settings)
    admin_engine = create_async_engine(settings.migration_database_url)
    factory = build_session_factory(engine)
    owner_tg = (uuid4().int % 7_000_000_000_000_000_000) + 1
    other_tg = (uuid4().int % 7_000_000_000_000_000) + 1
    latest = NOW - timedelta(minutes=10)

    async def fake_latest(bucket, *, now=None):
        assert bucket == "vimi-backup-test-bucket"
        return latest

    monkeypatch.setattr(monitor, "latest_gcs_manifest", fake_latest)
    try:
        owner = await ensure_telegram_actor(
            factory, telegram_id=owner_tg, username=None, first_name="Backup operator",
        )
        other = await ensure_telegram_actor(
            factory, telegram_id=other_tg, username=None, first_name="Not operator",
        )
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO public.platform_operators(telegram_user_id,role) "
                     "VALUES (:tid,'owner')"),
                {"tid": owner_tg},
            )

        assert await monitor.scan_platform_backup(
            factory, owner_actor_id=owner, bucket="vimi-backup-test-bucket",
            now=NOW,
        ) == 0
        healthy = await monitor.read_platform_backup_watch(
            factory, owner_actor_id=owner,
        )
        assert healthy["state"] == "healthy"
        assert await monitor.read_platform_backup_watch(
            factory, owner_actor_id=other,
        ) is None

        # Local systemd reporter writes only a sanitized outcome as the
        # postgres owner role; the central bot only reads it.
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO public.platform_backup_attempt_receipt "
                     "(id,reported_at,result) VALUES (1,:at,'failed') "
                     "ON CONFLICT(id) DO UPDATE SET reported_at=EXCLUDED.reported_at,"
                     "result=EXCLUDED.result"),
                {"at": NOW + timedelta(minutes=1)},
            )
        # An RLS runtime connection cannot forge the host's backup result,
        # even when CI grants DML on all public tables after Alembic.
        try:
            async with tenant_session_scope(
                factory, TenantContext(user_id=owner),
            ) as session:
                result = await session.execute(text(
                    "UPDATE public.platform_backup_attempt_receipt "
                    "SET result='success' WHERE id=1"
                ))
                assert result.rowcount == 0
        except DBAPIError:
            pass  # RLS may reject or silently filter UPDATE, depending on grants.
        async with admin_engine.connect() as conn:
            stored = await conn.scalar(text(
                "SELECT result FROM public.platform_backup_attempt_receipt WHERE id=1"
            ))
            assert stored == "failed"
        assert await monitor.scan_platform_backup(
            factory, owner_actor_id=owner, bucket="vimi-backup-test-bucket",
            now=NOW + timedelta(minutes=2),
        ) == 1
        assert await monitor.scan_platform_backup(
            factory, owner_actor_id=owner, bucket="vimi-backup-test-bucket",
            now=NOW + timedelta(minutes=3),
        ) == 0
        async with tenant_session_scope(
            factory, TenantContext(user_id=owner),
        ) as session:
            items = (await session.scalars(
                select(monitor.PlatformBackupAlertOutbox)
            )).all()
            assert len(items) == 1 and items[0].kind == "problem"
        telegram = SimpleNamespace(send_message=AsyncMock())
        assert await monitor.deliver_one_backup_notice(
            factory, bot=telegram, actor_id=owner, owner_telegram_id=owner_tg,
            timezone="Europe/Moscow", now=NOW + timedelta(minutes=4),
        )
        assert telegram.send_message.await_count == 1

        latest = NOW + timedelta(minutes=5)
        await monitor.scan_platform_backup(
            factory, owner_actor_id=owner, bucket="vimi-backup-test-bucket",
            now=NOW + timedelta(minutes=6),
        )
        assert await monitor.deliver_one_backup_notice(
            factory, bot=telegram, actor_id=owner, owner_telegram_id=owner_tg,
            timezone="Europe/Moscow", now=NOW + timedelta(minutes=7),
        )
        assert telegram.send_message.await_count == 2
        recovered = telegram.send_message.await_args.args[1].lower()
        assert "восстанов" in recovered or "backup recovered" in recovered
        assert not await monitor.deliver_one_backup_notice(
            factory, bot=telegram, actor_id=owner, owner_telegram_id=owner_tg,
            timezone="Europe/Moscow", now=NOW + timedelta(minutes=8),
        )
        healthy = await monitor.read_platform_backup_watch(
            factory, owner_actor_id=owner,
        )
        assert healthy["state"] == "healthy"

        # Intermittent GCS API errors must not restart a continuous stale
        # 26-hour incident confirmation window.
        late = NOW + timedelta(hours=27)
        assert await monitor.scan_platform_backup(
            factory, owner_actor_id=owner, bucket="vimi-backup-test-bucket",
            now=late,
        ) == 0  # first stale observation
        async def deny_list(bucket, *, now=None):
            raise monitor.BackupProbeError("temporary GCS error")

        monkeypatch.setattr(monitor, "latest_gcs_manifest", deny_list)
        assert await monitor.scan_platform_backup(
            factory, owner_actor_id=owner, bucket="vimi-backup-test-bucket",
            now=late + timedelta(minutes=5),
        ) == 0  # verification error, continuous problem
        monkeypatch.setattr(monitor, "latest_gcs_manifest", fake_latest)
        assert await monitor.scan_platform_backup(
            factory, owner_actor_id=owner, bucket="vimi-backup-test-bucket",
            now=late + timedelta(minutes=10),
        ) == 1  # stale confirmed despite intermittent GCS error
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(text(
                "DELETE FROM public.platform_operators WHERE telegram_user_id=:id"
            ), {"id": owner_tg})
        await engine.dispose()
        await admin_engine.dispose()
