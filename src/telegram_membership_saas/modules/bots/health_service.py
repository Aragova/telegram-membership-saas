"""Project-bound customer bot health without storing tokens or exception bodies."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.bots.diagnostics import HEALTH_ERROR_CODES
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth

STATES = frozenset({"setup_needed", "queued", "starting", "running", "stopped", "error"})
ERROR_CODES = HEALTH_ERROR_CODES


async def claim_bot_health(
    lease: AsyncConnection,
    *,
    project_id: UUID,
    bot_id: int,
    instance_id: UUID,
) -> None:
    """Claim startup state on the *same connection* holding the project lease.

    Every new owner must finish this write before releasing its advisory lock.
    A delayed old claim cannot run after a replacement acquires the lock.
    Other heartbeats cannot seize ownership; they update only their own ID.
    The lease connection is dedicated and returned only after worker teardown.
    """
    # Advisory-lease connections run AUTOCOMMIT, so this context must be
    # session-scoped instead of SET LOCAL. The connection is invalidated on
    # release and is never reused for another owner's project.
    await lease.execute(
        text("SELECT set_config('app.current_project_id', :project, false)"),
        {"project": str(project_id)},
    )
    now = datetime.now(UTC)
    stmt = insert(CustomerBotRuntimeHealth).values(
        project_id=project_id,
        telegram_bot_id=bot_id,
        instance_id=instance_id,
        state="starting",
        last_heartbeat=now,
        error_code=None,
    )
    await lease.execute(
        stmt.on_conflict_do_update(
            index_elements=[CustomerBotRuntimeHealth.project_id],
            set_={
                "telegram_bot_id": stmt.excluded.telegram_bot_id,
                "instance_id": stmt.excluded.instance_id,
                "state": stmt.excluded.state,
                "last_heartbeat": stmt.excluded.last_heartbeat,
                "error_code": stmt.excluded.error_code,
            },
        )
    )


async def write_bot_health(
    factory: async_sessionmaker[AsyncSession],
    *,
    project_id: UUID,
    bot_id: int,
    instance_id: UUID,
    state: str,
    error_code: str | None = None,
) -> None:
    if state not in STATES or (error_code is not None and error_code not in ERROR_CODES):
        raise ValueError("Invalid customer bot status")
    async with system_project_session_scope(factory, project_id) as session:
        values = {
            "project_id": project_id,
            "telegram_bot_id": bot_id,
            "instance_id": instance_id,
            "state": state,
            "last_heartbeat": datetime.now(UTC),
            "error_code": error_code,
        }
        stmt = insert(CustomerBotRuntimeHealth).values(**values)
        # A queued replica has NOT acquired the per-project advisory lease.
        # It must never replace the instance_id of an existing record, even
        # when the owning worker's heartbeat is temporarily stale. Otherwise
        # the real lease holder can no longer refresh its running status.
        if state == "queued":
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[CustomerBotRuntimeHealth.project_id],
            )
        else:
            # Even "starting" heartbeats must not seize another instance's
            # row. Ownership changes ONLY in claim_bot_health(), which uses
            # the project's actual advisory-lock connection.
            allowed = CustomerBotRuntimeHealth.instance_id == instance_id
            stmt = stmt.on_conflict_do_update(
                index_elements=[CustomerBotRuntimeHealth.project_id],
                set_={
                    "telegram_bot_id": stmt.excluded.telegram_bot_id,
                    "instance_id": stmt.excluded.instance_id,
                    "state": stmt.excluded.state,
                    "last_heartbeat": stmt.excluded.last_heartbeat,
                    "error_code": stmt.excluded.error_code,
                },
                where=allowed,
            )
        await session.execute(stmt)


async def read_bot_health(
    session: AsyncSession,
    *,
    project_id: UUID,
) -> CustomerBotRuntimeHealth | None:
    return await session.scalar(
        select(CustomerBotRuntimeHealth).where(
            CustomerBotRuntimeHealth.project_id == project_id
        )
    )


def public_health_label(
    health: CustomerBotRuntimeHealth | None,
    *,
    telegram_bot_id: int,
    now: datetime | None = None,
) -> tuple[str, bool]:
    """Never advertise a bot link as live based on a stale worker heartbeat."""
    moment = now or datetime.now(UTC)
    if health is None or health.telegram_bot_id != telegram_bot_id:
        return "⚪ Ожидает запуска", False
    if (moment - health.last_heartbeat).total_seconds() > 40:
        return "⚪ Нет связи с ботом", False
    if health.state == "running":
        return "🟢 Работает", True
    if health.state == "starting":
        return "🟡 Запускается", False
    if health.state == "queued":
        return "🟡 В очереди на запуск", False
    if health.state == "setup_needed":
        return "🟡 Требуется настройка", False
    if health.state == "error":
        labels = {
            "invalid_credentials": "🔴 Ошибка токена — подключите бота повторно",
            "telegram_check_failed": "🔴 Проверьте токен и webhook бота",
            "identity_mismatch": "🔴 getMe: ID бота не совпадает",
            "identity_network_failed": "🔴 getMe: сетевая ошибка Telegram",
            "identity_api_failed": "🔴 getMe: ошибка Telegram API",
            "identity_check_failed": "🔴 getMe: проверка не завершена",
            "webhook_active": "🔴 Найден активный webhook",
            "webhook_network_failed": "🔴 getWebhookInfo: сетевая ошибка",
            "webhook_api_failed": "🔴 getWebhookInfo: ошибка Telegram API",
            "webhook_check_failed": "🔴 getWebhookInfo: проверка не завершена",
            "polling_network_failed": "🔴 getUpdates: обрыв соединения",
            "polling_api_failed": "🔴 getUpdates: ошибка Telegram API",
            "polling_conflict": "🔴 getUpdates: конфликт polling",
            "startup_stalled": "🔴 Запуск бота: превышен срок подготовки",
            "polling_stalled": "🔴 getUpdates: нет ответов",
            "polling_handler_stalled": "🔴 Telegram: обработчик задержал polling",
            "polling_check_failed": "🔴 getUpdates: ошибка опроса",
            "missing_admin_rights": "🔴 Проверьте права бота в канале",
            "resource_network_failed": "🔴 Проверка прав: ошибка сети Telegram",
            "resource_api_failed": "🔴 Проверка прав: ошибка Telegram API",
            "resource_rate_limited": "🟡 Проверка прав: ограничение Telegram",
            "resource_check_failed": "🔴 Проверка прав не завершена",
            "resource_access_denied": "🔴 Telegram отклонил доступ к каналу",
            "identity_rate_limited": "🟡 getMe: ограничение Telegram",
            "webhook_rate_limited": "🟡 webhook: ограничение Telegram",
            "lock_lost": "🟡 Бот перезапускается",
            "worker_stopped": "🟡 Бот перезапускается",
        }
        return labels.get(health.error_code, "🔴 Не удалось запустить бота"), False
    return "⚪ Не запущен", False
