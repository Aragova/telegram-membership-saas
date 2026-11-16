"""One GCS backup freshness incident stream inside existing central monitoring.

Read-only Cloud Storage API probe using the VM metadata access token. The
default VM service account must be objectViewer on the encrypted backup bucket;
the GCS writer identity remains objectCreator only. No keys or tokens in logs.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from urllib.parse import quote

import httpx
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import DateTime, Integer, String, Uuid, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.db_base import Base
from telegram_membership_saas.localization import recipient_language, tr
from telegram_membership_saas.telegram_dates import native_telegram_time
from telegram_membership_saas.tenant_context import TenantContext

log = logging.getLogger(__name__)

FRESH_FOR = timedelta(hours=26)
VERIFY_ERROR_AFTER = timedelta(minutes=15)
STALE_CONFIRM_AFTER = timedelta(minutes=5)
LEASE = timedelta(minutes=5)
PREFIX = "vimi-pay/postgres/"
MANIFEST_RE = re.compile(
    r"^vimi-pay/postgres/\d{8}T\d{6}Z-[0-9a-f-]{36}/manifest\.sha256$"
)
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)


class BackupProbeError(Exception):
    """A failed independent GCS verification, never a claim of missing backups."""


class PlatformBackupAttemptReceipt(Base):
    __tablename__ = "platform_backup_attempt_receipt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)


class PlatformBackupWatch(Base):
    __tablename__ = "platform_backup_watch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_complete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspected_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    incident_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    last_resolved_incident_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))


class PlatformBackupAlertOutbox(Base):
    __tablename__ = "platform_backup_alert_outbox"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _manifest_created(item: object, *, now: datetime) -> datetime | None:
    if not isinstance(item, dict) or not MANIFEST_RE.fullmatch(str(item.get("name", ""))):
        return None
    raw = item.get("timeCreated")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    value = value.astimezone(UTC)
    return value if value <= now + timedelta(minutes=5) else None


async def latest_gcs_manifest(bucket: str, *, now: datetime | None = None) -> datetime | None:
    """List ALL completed manifest markers, not bare .age objects or local logs.

    Complete marker is uploaded last by vimi-pay-gcs-backup. If listing cannot
    finish, return an error instead of using a misleading partial page.
    """
    moment = now or datetime.now(UTC)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,61}", bucket):
        raise BackupProbeError("Invalid configured GCS bucket")
    latest: datetime | None = None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0), trust_env=False,
        ) as client:
            metadata = await client.get(
                METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"},
            )
            metadata.raise_for_status()
            token = metadata.json()["access_token"]
            if not isinstance(token, str) or not token:
                raise ValueError("Invalid metadata token")
            params = {
                "prefix": PREFIX,
                "fields": "nextPageToken,items(name,timeCreated)",
                "maxResults": "1000",
            }
            for _ in range(10):
                response = await client.get(
                    "https://storage.googleapis.com/storage/v1/b/"
                    + quote(bucket, safe="") + "/o",
                    headers={"Authorization": "Bearer " + token},
                    params=params,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Invalid GCS response")
                for item in data.get("items", []):
                    stamp = _manifest_created(item, now=moment)
                    if stamp is not None and (latest is None or stamp > latest):
                        latest = stamp
                next_token = data.get("nextPageToken")
                if next_token is None:
                    return latest
                if not isinstance(next_token, str) or not next_token:
                    raise ValueError("Invalid GCS pagination")
                params["pageToken"] = next_token
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        # Never log bearer credentials, response bodies, or exception URLs.
        raise BackupProbeError("GCS manifest verification unavailable") from None
    raise BackupProbeError("GCS listing exceeds safe page bound")


def _condition(
    latest: datetime | None, *, verified: bool,
    receipt: PlatformBackupAttemptReceipt | None, now: datetime,
) -> str:
    # Explicit systemd failure wins until a newer completed cloud upload.
    if (
        receipt is not None and receipt.result == "failed"
        and (latest is None or receipt.reported_at > latest)
    ):
        return "failed"
    if not verified:
        return "verify_error"
    if latest is None or now - latest > FRESH_FOR:
        return "stale"
    return "healthy"


def _new_event(incident: UUID, kind: str, reason: str, now: datetime):
    return PlatformBackupAlertOutbox(
        id=uuid4(), incident_id=incident, kind=kind, reason=reason,
        status="pending", attempts=0, available_at=now,
    )


async def scan_platform_backup(
    factory: async_sessionmaker[AsyncSession], *,
    owner_actor_id: UUID, bucket: str, now: datetime | None = None,
) -> int:
    """Persist one alert per outage, and close it after verified recovery."""
    moment = now or datetime.now(UTC)
    verified = True
    try:
        latest = await latest_gcs_manifest(bucket, now=moment)
    except BackupProbeError:
        verified, latest = False, None

    async with tenant_session_scope(
        factory, TenantContext(user_id=owner_actor_id),
    ) as session:
        # Prevent overlapping central bot instances from racing. I/O happens
        # BEFORE this transaction, so the transaction remains short.
        locked = await session.scalar(
            __import__("sqlalchemy").text("SELECT pg_try_advisory_xact_lock(1891173522)")
        )
        if not locked:
            return 0
        watch = await session.get(PlatformBackupWatch, 1, with_for_update=True)
        if watch is None:
            watch = PlatformBackupWatch(id=1, state="healthy")
            session.add(watch)
            await session.flush()
        receipt = await session.get(PlatformBackupAttemptReceipt, 1)
        # On a failed GCS read, preserve the last confirmed manifest time.
        if verified:
            watch.last_complete_at = latest
        last_known = watch.last_complete_at
        result = _condition(last_known, verified=verified, receipt=receipt, now=moment)
        previous = watch.state
        watch.checked_at = moment
        if result == "healthy":
            if watch.incident_id is not None:
                incident = watch.incident_id
                events = (
                    await session.scalars(
                        select(PlatformBackupAlertOutbox)
                        .where(PlatformBackupAlertOutbox.incident_id == incident)
                        .with_for_update()
                    )
                ).all()
                problem = next((e for e in events if e.kind == "problem"), None)
                if problem is not None:
                    if problem.status in {"pending", "sending"}:
                        problem.status = "cancelled"
                    if problem.status == "sent" or problem.attempts > 0:
                        session.add(_new_event(incident, "recovered", "healthy", moment))
                watch.last_resolved_incident_id = incident
                watch.incident_id = None
            watch.state = "healthy"
            watch.suspected_since = None
            return 0

        if previous != result and watch.incident_id is None:
            watch.suspected_since = moment
        watch.state = result
        watch.suspected_since = watch.suspected_since or moment
        threshold = (
            VERIFY_ERROR_AFTER if result == "verify_error"
            else STALE_CONFIRM_AFTER if result == "stale" else timedelta(0)
        )
        if watch.incident_id is None and moment - watch.suspected_since >= threshold:
            watch.incident_id = uuid4()
            watch.last_resolved_incident_id = None
            session.add(_new_event(watch.incident_id, "problem", result, moment))
            return 1
        return 0


async def read_platform_backup_watch(
    factory: async_sessionmaker[AsyncSession], *, owner_actor_id: UUID,
) -> dict | None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner_actor_id),
    ) as session:
        watch = await session.get(PlatformBackupWatch, 1)
        return (
            {"state": watch.state, "last_complete_at": watch.last_complete_at,
             "checked_at": watch.checked_at}
            if watch is not None else None
        )


def backup_monitor_lines(health: dict | None, *, timezone: str) -> list[str]:
    """Append two text lines to EXISTING Monitoring screen, no new controls."""
    if health is None:
        return [tr("🔐 Резервная копия: ожидание проверки GCS",
                   "🔐 Backup: awaiting GCS verification")]
    state = health["state"]
    label = {
        "healthy": tr("🟢 Резервная копия: актуальна", "🟢 Backup: current"),
        "stale": tr("🔴 Резервная копия: старше 26 ч",
                    "🔴 Backup: older than 26h"),
        "failed": tr("🔴 Резервная копия: ошибка задания",
                     "🔴 Backup: job failed"),
        "verify_error": tr("🟠 Резервная копия: GCS недоступен для проверки",
                           "🟠 Backup: GCS verification unavailable"),
    }.get(state, tr("🟠 Резервная копия: не проверена",
                     "🟠 Backup: not verified"))
    result = [label]
    last = health.get("last_complete_at")
    if last is not None:
        result.append(
            tr("Последний архив: ", "Last archive: ")
            + native_telegram_time(last, timezone)
        )
    return result


def _message(kind: str, reason: str, *, last: datetime | None, timezone: str) -> str:
    if kind == "recovered":
        header = tr("✅ <b>ViMi Pay · Резервное копирование восстановлено</b>",
                    "✅ <b>ViMi Pay · Backup recovered</b>")
        detail = tr("Новая завершённая копия подтверждена в GCS.",
                    "A new completed backup was verified in GCS.")
    else:
        header = tr("🔴 <b>ViMi Pay · Проблема с резервной копией</b>",
                    "🔴 <b>ViMi Pay · Backup incident</b>")
        detail = {
            "failed": tr("Системное задание завершилось ошибкой.",
                         "The scheduled backup job failed."),
            "stale": tr("Нет подтверждённой копии в GCS за последние 26 часов.",
                        "No confirmed GCS backup in the last 26 hours."),
            "verify_error": tr("Проверка GCS недоступна более 15 минут.",
                               "GCS verification has failed for over 15 minutes."),
        }.get(reason, tr("Требуется проверка резервных копий.",
                          "Backups require inspection."))
    lines = [header, detail]
    if last is not None:
        lines.append(
            tr("Последний архив: ", "Last archive: ")
            + native_telegram_time(last, timezone)
        )
    lines.append(tr("Проверьте раздел 🖥 Мониторинг.",
                    "Check 🖥 Monitoring."))
    return "\n".join(lines)


async def _claim(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, now: datetime,
) -> tuple[UUID, int, str, str, datetime | None] | None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        event = await session.scalar(
            select(PlatformBackupAlertOutbox)
            .where(
                PlatformBackupAlertOutbox.available_at <= now,
                or_(
                    PlatformBackupAlertOutbox.status == "pending",
                    and_(
                        PlatformBackupAlertOutbox.status == "sending",
                        PlatformBackupAlertOutbox.claimed_at < now - LEASE,
                    ),
                ),
            ).order_by(
                PlatformBackupAlertOutbox.available_at,
                PlatformBackupAlertOutbox.id,
            ).limit(1).with_for_update(skip_locked=True)
        )
        if event is None:
            return None
        watch = await session.get(PlatformBackupWatch, 1)
        if (
            watch is None or
            (event.kind == "problem" and watch.incident_id != event.incident_id) or
            (event.kind == "recovered" and (
                watch.state != "healthy" or
                watch.last_resolved_incident_id != event.incident_id
            ))
        ):
            event.status = "cancelled"
            return None
        event.status = "sending"
        event.attempts += 1
        event.claimed_at = now
        return (
            event.id, event.attempts, event.kind, event.reason,
            watch.last_complete_at,
        )


async def _finish(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, event_id: UUID, attempt: int, delivered: bool,
    now: datetime,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        await session.execute(
            update(PlatformBackupAlertOutbox)
            .where(
                PlatformBackupAlertOutbox.id == event_id,
                PlatformBackupAlertOutbox.status == "sending",
                PlatformBackupAlertOutbox.attempts == attempt,
            )
            .values(**(
                {"status": "sent", "delivered_at": now}
                if delivered else {
                    "status": "pending",
                    "available_at": now + timedelta(
                        seconds=min(3600, 60 * 2 ** min(attempt, 6))
                    ),
                }
            ))
        )


async def deliver_one_backup_notice(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, actor_id: UUID, owner_telegram_id: int,
    timezone: str, now: datetime | None = None,
) -> bool:
    moment = now or datetime.now(UTC)
    event = await _claim(factory, actor_id=actor_id, now=moment)
    if event is None:
        return False
    event_id, attempt, kind, reason, last = event
    sent = False
    try:
        async with recipient_language(factory, owner_telegram_id):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr("🖥 Мониторинг", "🖥 Monitoring"),
                    callback_data="plat:bots:30:0",
                )
            ]])
            await bot.send_message(
                owner_telegram_id,
                _message(kind, reason, last=last, timezone=timezone),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        sent = True
    except Exception:
        log.warning("Backup monitoring notification deferred: kind=%s", kind)
    finally:
        await _finish(
            factory, actor_id=actor_id, event_id=event_id, attempt=attempt,
            now=moment, delivered=sent,
        )
    return sent
