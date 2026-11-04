"""Per-project PostgreSQL session advisory lock for Telegram pollers.

A dedicated AUTOCOMMIT connection owns the lock for the full poller lifetime.
Heartbeat reacquires on the *same* connection. If the connection is replaced
after failover and another worker holds the lock, heartbeat fails closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession


class ProjectWorkerBusy(RuntimeError):
    pass


def project_lock_key(project_id: UUID) -> int:
    digest = sha256(b"vimi-telegram-access-v1:" + project_id.bytes).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def checkout_retirement_lock(
    session: AsyncSession, project_id: UUID,
) -> None:
    """Serialize new checkouts with owner bot-retirement requests.

    Uses a distinct advisory key from the **session-scoped** running bot
    worker lease, so payments never block behind healthy customer pollers.
    The transaction-scoped lock is released automatically at commit.
    """
    digest = sha256(b"vimi-checkout-retirement-v1:" + project_id.bytes).digest()
    key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": key},
    )


async def confirm_project_lock(conn: AsyncConnection, project_id: UUID) -> None:
    value = await conn.scalar(
        text("SELECT pg_try_advisory_lock(:key)"),
        {"key": project_lock_key(project_id)},
    )
    if not value:
        raise ProjectWorkerBusy("A project access worker already holds this lease")


@asynccontextmanager
async def project_worker_lock(
    engine: AsyncEngine, project_id: UUID,
) -> AsyncIterator[AsyncConnection]:
    connection = await engine.connect()
    try:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await confirm_project_lock(connection, project_id)
        yield connection
    finally:
        # Avoid returning a connection to the pool with reentrant advisory
        # locks still held after repeated heartbeat verification.
        await connection.invalidate()
        await connection.close()
