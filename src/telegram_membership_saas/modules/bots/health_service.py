"""Project-bound customer bot health without storing tokens or exception bodies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.db import system_project_session_scope
from telegram_membership_saas.modules.bots.runtime_health import CustomerBotRuntimeHealth

STATES = frozenset({"setup_needed", "queued", "starting", "running", "stopped", "error"})
ERROR_CODES = frozenset({
    "startup_failed", "invalid_credentials", "telegram_check_failed",
    "missing_admin_rights", "worker_stopped", "lock_lost",
})


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
        # A finishing old worker cannot overwrite a different worker's status.
        # A new startup (after obtaining the advisory lock) may take ownership.
        if state == "queued":
            # Other supervisor replicas may already be running the project.
            # Never overwrite their recent live heartbeat with 'queued'.
            allowed = (
                CustomerBotRuntimeHealth.last_heartbeat
                < datetime.now(UTC) - timedelta(seconds=45)
            )
        else:
            allowed = (
                True if state in {"starting", "setup_needed"} else
                CustomerBotRuntimeHealth.instance_id == instance_id
            )
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
            "missing_admin_rights": "🔴 Проверьте права бота в канале",
            "lock_lost": "🟡 Бот перезапускается",
            "worker_stopped": "🟡 Бот перезапускается",
        }
        return labels.get(health.error_code, "🔴 Не удалось запустить бота"), False
    return "⚪ Не запущен", False
