"""One platform-owner incident scanner piggybacking on the CENTRAL bot runtime.

Observes Supervisor's persisted health; never polls customer bots or performs
access/payment actions. Owner-only RLS, audited read and durable event outbox.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID, uuid4

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Uuid,
    and_,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.config import Settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.db_base import Base
from telegram_membership_saas.localization import recipient_language, tr
from telegram_membership_saas.modules.identity.models import TelegramUser
from telegram_membership_saas.modules.bots.diagnostics import HEALTH_ERROR_CODES
from telegram_membership_saas.modules.platform_saas_notices import (
    deliver_one_saas_notice,
    schedule_saas_reminders,
)
from telegram_membership_saas.modules.platform_saas_proof_alerts import (
    deliver_one_platform_proof_alert,
)
from telegram_membership_saas.modules.platform_saas_review_results import (
    deliver_one_saas_review_result,
)
from telegram_membership_saas.telegram_dates import native_telegram_time
from telegram_membership_saas.tenant_context import TenantContext

log = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)
ALERT_AFTER = timedelta(minutes=10)
SLOW_START_ALERT_AFTER = timedelta(minutes=20)
HEALTHY_FOR_RECOVERY = timedelta(minutes=2)
HEARTBEAT_MAX_AGE = timedelta(seconds=120)
DELIVERY_LEASE = timedelta(minutes=5)

SAFE_CODES = HEALTH_ERROR_CODES


class PlatformMonitorIncident(Base):
    __tablename__ = "platform_monitor_incidents"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    bot_instance_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bot_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    suspected_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    last_runtime_state: Mapped[str] = mapped_column(String(24), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    healthy_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformMonitorOutbox(Base):
    __tablename__ = "platform_monitor_alert_outbox"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("platform_monitor_incidents.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _observed_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None


def _healthy(observation: dict, now: datetime) -> bool:
    beat = _observed_time(observation.get("last_heartbeat"))
    if observation.get("status") != "registered" or beat is None:
        return False
    return (
        observation.get("runtime_state") == "running"
        and timedelta(seconds=-60) <= now - beat <= HEARTBEAT_MAX_AGE
    )


def _problem_threshold(observation: dict) -> timedelta | None:
    """No alarm for intentional disable, deletion or owner setup needs."""
    if observation.get("status") != "registered":
        return None
    state = observation.get("runtime_state")
    if state == "setup_needed":
        return None
    if state in {"queued", "starting"}:
        return SLOW_START_ALERT_AFTER
    return ALERT_AFTER


def _clean_code(code: object) -> str | None:
    return code if isinstance(code, str) and code in SAFE_CODES else None


async def _platform_actor(
    factory: async_sessionmaker[AsyncSession], owner_telegram_id: int,
) -> UUID | None:
    async with factory() as session:
        return await session.scalar(
            select(TelegramUser.id)
            .where(TelegramUser.telegram_user_id == owner_telegram_id)
        )


def _new_event(
    incident_id: UUID, kind: str, *, now: datetime,
) -> PlatformMonitorOutbox:
    return PlatformMonitorOutbox(
        id=uuid4(), incident_id=incident_id, kind=kind,
        status="pending", attempts=0, available_at=now,
    )


async def scan_platform_monitor(
    factory: async_sessionmaker[AsyncSession], *,
    owner_actor_id: UUID,
    now: datetime | None = None,
) -> int:
    """Persist repeated health observations; emit only stable incident events.

    pg_try_advisory_xact_lock serializes scans even during overlapping central
    bot rollouts. Telegram is NEVER called inside this SQL transaction.
    """
    moment = now or datetime.now(UTC)
    async with tenant_session_scope(
        factory, TenantContext(user_id=owner_actor_id),
    ) as session:
        acquired = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(1891173501)")
        )
        if not acquired:
            return 0
        snapshot = (await session.execute(
            text("SELECT public.platform_monitor_health_snapshot() AS data")
            .columns(data=JSONB),
        )).scalar_one()
        if not isinstance(snapshot, list):
            raise RuntimeError("Invalid monitored bot observations")
        active = (
            await session.scalars(
                select(PlatformMonitorIncident)
                .where(PlatformMonitorIncident.recovered_at.is_(None))
                .with_for_update()
            )
        ).all()
        by_bot = {incident.bot_instance_id: incident for incident in active}
        created = 0
        observed_ids: set[UUID] = set()

        for bot in snapshot:
            if not isinstance(bot, dict):
                raise RuntimeError("Invalid monitored bot row")
            bot_id = UUID(str(bot["bot_instance_id"]))
            observed_ids.add(bot_id)
            incident = by_bot.get(bot_id)
            healthy = _healthy(bot, moment)
            threshold = _problem_threshold(bot)
            if healthy or threshold is None:
                if incident is None:
                    continue
                events = {
                    ev.kind: ev for ev in (
                        await session.scalars(
                            select(PlatformMonitorOutbox)
                            .where(PlatformMonitorOutbox.incident_id == incident.id)
                            .with_for_update()
                        )
                    ).all()
                }
                problem = events.get("problem")
                if threshold is None or problem is None:
                    if problem is not None and problem.status == "pending":
                        problem.status = "cancelled"
                    incident.recovered_at = moment
                    continue
                if problem.status == "pending" and problem.attempts == 0:
                    # Never attempted: cancel cleanly, with no extra message.
                    problem.status = "cancelled"
                    incident.recovered_at = moment
                    continue

                incident.healthy_since = incident.healthy_since or moment
                if moment - incident.healthy_since < HEALTHY_FOR_RECOVERY:
                    continue
                if problem.status == "pending":
                    # An earlier attempt may actually have reached Telegram
                    # before the transport failed. Once recovered, stop retries
                    # and provide the one possible closing notification.
                    problem.status = "cancelled"
                elif problem.status == "sending":
                    # The sending process may have died after claiming. Once
                    # its lease expires and recovery is stable, do NOT replay
                    # an already-obsolete problem. Whether it sent is unknown;
                    # a recovery message safely closes that possibility.
                    if (
                        problem.claimed_at is None
                        or moment - problem.claimed_at < DELIVERY_LEASE
                    ):
                        continue
                    problem.status = "cancelled"
                elif problem.status not in {"sent", "cancelled"}:
                    continue
                incident.recovered_at = moment
                if problem.status == "sent" or problem.attempts > 0:
                    session.add(_new_event(incident.id, "recovered", now=moment))
                    created += 1
                continue

            if incident is None:
                incident = PlatformMonitorIncident(
                    id=uuid4(), bot_instance_id=bot_id,
                    username=str(bot.get("username") or "unknown")[:64],
                    suspected_since=moment, last_observed_at=moment,
                    last_runtime_state=str(bot.get("runtime_state") or "unknown")[:24],
                    last_error_code=_clean_code(bot.get("error_code")),
                    last_heartbeat=_observed_time(bot.get("last_heartbeat")),
                )
                session.add(incident)
                by_bot[bot_id] = incident
            else:
                incident.last_observed_at = moment
                incident.username = str(bot.get("username") or "unknown")[:64]
                incident.last_runtime_state = str(
                    bot.get("runtime_state") or "unknown"
                )[:24]
                incident.last_error_code = _clean_code(bot.get("error_code"))
                incident.last_heartbeat = _observed_time(bot.get("last_heartbeat"))
                incident.healthy_since = None

            if moment - incident.suspected_since < threshold:
                continue
            previous = await session.scalar(select(PlatformMonitorOutbox.id).where(
                PlatformMonitorOutbox.incident_id == incident.id,
                PlatformMonitorOutbox.kind == "problem",
            ))
            if previous is None:
                incident.confirmed_at = moment
                session.add(_new_event(incident.id, "problem", now=moment))
                created += 1

        # Removed connections have no active alert, even when old metadata
        # remains before the FK cascade cleanup.
        for incident in active:
            if incident.bot_instance_id not in observed_ids:
                incident.recovered_at = moment
                await session.execute(
                    text(
                        "UPDATE public.platform_monitor_alert_outbox "
                        "SET status = 'cancelled' WHERE incident_id = :id "
                        "AND kind = 'problem' AND status = 'pending'"
                    ), {"id": incident.id},
                )
        return created


async def _claim_event(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, now: datetime,
) -> tuple[UUID, int, str, str, str, str | None, datetime | None, datetime] | None:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        event = await session.scalar(
            select(PlatformMonitorOutbox)
            .where(
                PlatformMonitorOutbox.available_at <= now,
                or_(
                    PlatformMonitorOutbox.status == "pending",
                    and_(
                        PlatformMonitorOutbox.status == "sending",
                        PlatformMonitorOutbox.claimed_at < now - DELIVERY_LEASE,
                    ),
                ),
            )
            .order_by(PlatformMonitorOutbox.available_at, PlatformMonitorOutbox.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if event is None:
            return None
        incident = await session.get(PlatformMonitorIncident, event.incident_id)
        if incident is None or (
            event.kind == "problem" and incident.recovered_at is not None
        ) or (
            event.kind == "recovered" and incident.recovered_at is None
        ):
            event.status = "cancelled"
            return None
        if (
            event.kind == "problem"
            and incident.healthy_since is not None
            and now - incident.healthy_since >= HEALTHY_FOR_RECOVERY
        ):
            # An expired delivery lease never resurrects a stale alert once
            # a sustained recovery has already been observed in the DB.
            event.status = "cancelled"
            return None
        if event.kind == "recovered":
            newer_episode = await session.scalar(
                select(PlatformMonitorIncident.id).where(
                    PlatformMonitorIncident.bot_instance_id == incident.bot_instance_id,
                    PlatformMonitorIncident.suspected_since > incident.suspected_since,
                ).limit(1)
            )
            if newer_episode is not None:
                # Do not announce a stale "recovered" event after a newer
                # outage has begun or its red alert was sent.
                event.status = "cancelled"
                return None
        event.status = "sending"
        event.attempts += 1
        event.claimed_at = now
        return (
            event.id, event.attempts, event.kind, incident.username, incident.last_runtime_state,
            incident.last_error_code, incident.last_heartbeat,
            incident.suspected_since,
        )


async def _finish_event(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID,
    event_id: UUID, claim_attempt: int, now: datetime, delivered: bool,
) -> None:
    """Only the current lease owner can acknowledge a delivery attempt.

    The monotonically increasing attempts value is the fencing generation.
    A sender that outlives its lease cannot alter a replacement sender's
    in-flight event or cause a duplicate retry after successful delivery.
    """
    values = (
        {"status": "sent", "delivered_at": now}
        if delivered else
        {
            "status": "pending",
            "available_at": now + timedelta(
                seconds=min(3600, 60 * 2 ** min(claim_attempt, 6))
            ),
        }
    )
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        await session.execute(
            update(PlatformMonitorOutbox)
            .where(
                PlatformMonitorOutbox.id == event_id,
                PlatformMonitorOutbox.status == "sending",
                PlatformMonitorOutbox.attempts == claim_attempt,
            )
            .values(**values)
        )


def _alert_text(
    kind: str, *, username: str, status: str,
    error_code: str | None, heartbeat: datetime | None,
    since: datetime, timezone: str,
) -> str:
    from telegram_membership_saas.modules.platform_dashboard_handlers import (
        _ERROR_TEXT,
    )

    name = escape("@" + username.lstrip("@"))
    if kind == "recovered":
        return (
            tr("✅ <b>ViMi Pay · Работа восстановлена</b>",
               "✅ <b>ViMi Pay · Bot recovered</b>")
            + "\n🤖 <b>" + name + "</b>\n"
            + tr("Бот снова отвечает, восстановление подтверждено повторной проверкой.",
                 "The bot is responding again; recovery was verified by repeat checks.")
        )
    reason = (
        tr(*_ERROR_TEXT[error_code])
        if error_code in _ERROR_TEXT else
        tr("Supervisor длительно не подтверждает работу",
           "Supervisor has not confirmed runtime for an extended period")
    )
    last = (
        native_telegram_time(heartbeat, timezone)
        if heartbeat is not None else tr("Нет данных", "No data")
    )
    return (
        tr("🔴 <b>ViMi Pay · Устойчивая проблема</b>",
           "🔴 <b>ViMi Pay · Persistent bot issue</b>")
        + "\n🤖 <b>" + name + "</b>\n"
        + tr("Состояние: ", "Runtime: ") + escape(status) + "\n"
        + tr("Причина: ", "Reason: ") + escape(reason) + "\n"
        + tr("Последний сигнал: ", "Last heartbeat: ") + last + "\n"
        + tr("Наблюдаем с: ", "Observed since: ")
        + native_telegram_time(since, timezone)
    )


async def deliver_platform_monitor_event(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, owner_actor_id: UUID, owner_telegram_id: int,
    timezone: str, now: datetime | None = None,
) -> bool:
    """Send one durable alert/recovery, with backoff and same-screen CTA."""
    moment = now or datetime.now(UTC)
    event = await _claim_event(factory, actor_id=owner_actor_id, now=moment)
    if event is None:
        return False
    event_id, claim_attempt, kind, username, state, code, heartbeat, since = event
    delivered = False
    try:
        async with recipient_language(factory, owner_telegram_id):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr("🖥 Мониторинг", "🖥 Monitoring"),
                    callback_data="plat:bots:30:0",
                ),
            ]])
            message = _alert_text(
                kind, username=username, status=state,
                error_code=code, heartbeat=heartbeat, since=since,
                timezone=timezone,
            )
            await bot.send_message(
                owner_telegram_id, message, parse_mode="HTML",
                reply_markup=keyboard,
            )
        delivered = True
    except Exception:
        # No raw exceptions or Telegram URLs (which may contain tokens) in logs.
        log.warning("Platform monitor alert delivery deferred: kind=%s", kind)
    finally:
        await _finish_event(
            factory, actor_id=owner_actor_id,
            event_id=event_id, claim_attempt=claim_attempt,
            now=moment, delivered=delivered,
        )
    return delivered


async def run_central_platform_monitor(
    factory: async_sessionmaker[AsyncSession], *,
    bot: Bot, settings: Settings,
) -> None:
    """One small task inside the existing central bot, NOT another Supervisor."""
    owner_tg = settings.platform_owner_telegram_id
    if owner_tg is None:
        return
    while True:
        try:
            actor = await _platform_actor(factory, owner_tg)
            if actor is not None:
                # New bank proof notifications belong to the CENTRAL chat.
                # Process them before other monitoring so a failing health
                # scan cannot silently hide a payment awaiting review.
                try:
                    for _ in range(3):
                        if not await deliver_one_platform_proof_alert(
                            factory, bot=bot, platform_actor_id=actor,
                            owner_telegram_id=owner_tg,
                        ):
                            break
                except Exception:
                    log.warning("Central payment-proof notification deferred")
                # A decision must also reach the paying admin in the
                # ORIGINAL bot chat, without creating a new supervisor.
                try:
                    for _ in range(3):
                        if not await deliver_one_saas_review_result(
                            factory, central_bot=bot, settings=settings,
                            actor_id=actor,
                        ):
                            break
                except Exception:
                    log.warning("ViMi Pay customer review-result delivery deferred")
                await scan_platform_monitor(factory, owner_actor_id=actor)
                # Bounded delivery, never overwhelm the owner's chat on
                # recovery of a large network.
                for _ in range(3):
                    if not await deliver_platform_monitor_event(
                        factory, bot=bot, owner_actor_id=actor,
                        owner_telegram_id=owner_tg,
                        timezone=settings.platform_timezone,
                    ):
                        break
                # Platform billing notices share the SAME central process.
                # Never let a reminder failure interfere with bot monitoring.
                try:
                    await schedule_saas_reminders(
                        factory, platform_actor_id=actor,
                    )
                    for _ in range(3):
                        if not await deliver_one_saas_notice(
                            factory, bot=bot, platform_actor_id=actor,
                        ):
                            break
                except Exception:
                    log.warning("Platform SaaS reminder scan/delivery deferred")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Platform monitor scan/delivery failed; will retry")
        await asyncio.sleep(SCAN_INTERVAL.total_seconds())
