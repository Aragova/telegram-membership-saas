"""Per-project PostgreSQL session advisory lock for Telegram pollers.

A dedicated AUTOCOMMIT connection owns the lock for the full poller lifetime.
Heartbeat reacquires on the *same* connection. If the connection is replaced
after failover and another worker holds the lock, heartbeat fails closed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
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


async def confirm_project_lock(
    conn: AsyncConnection | SharedProjectLeaseHandle, project_id: UUID,
) -> None:
    if isinstance(conn, SharedProjectLeaseHandle):
        await conn.confirm(project_id)
        return
    value = await conn.scalar(
        text("SELECT pg_try_advisory_lock(:key)"),
        {"key": project_lock_key(project_id)},
    )
    if not value:
        raise ProjectWorkerBusy("A project access worker already holds this lease")


@asynccontextmanager
async def project_worker_lock(
    engine: AsyncEngine | SharedProjectLeaseManager, project_id: UUID,
) -> AsyncIterator[AsyncConnection | SharedProjectLeaseHandle]:
    if isinstance(engine, SharedProjectLeaseManager):
        async with engine.project_lock(project_id) as handle:
            yield handle
        return

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


class SharedProjectLeaseHandle:
    """One project-scoped view of the supervisor's shared PG session.

    Every statement is serialized and rebinds RLS to this project. This is
    essential: PostgreSQL session settings must not leak between simultaneous
    bot startup/health tasks.
    """

    def __init__(self, manager: SharedProjectLeaseManager, project_id: UUID) -> None:
        self._manager = manager
        self.project_id = project_id

    async def execute(self, statement, parameters=None):
        return await self._manager.execute_scoped(
            self.project_id, statement, parameters,
        )

    async def scalar(self, statement, parameters=None):
        result = await self.execute(statement, parameters)
        return result.scalar()

    async def confirm(self, project_id: UUID) -> None:
        if project_id != self.project_id:
            raise ProjectWorkerBusy("Lease project identity mismatch")
        await self._manager.confirm(project_id)


class SharedProjectLeaseManager:
    """Multiplex independent project advisory locks onto ONE DB connection.

    Opt-in replacement for N idle lease sessions, not a new ownership model.
    Competing legacy pollers and supervisors still use the exact same keys.
    All SQL on the shared connection is serialized; the backend PID is checked
    before every operation to detect lost/reconnected sessions and fail closed.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._mutex = asyncio.Lock()
        self._connection: AsyncConnection | None = None
        self._backend_pid: int | None = None
        self._held: set[UUID] = set()
        self._broken = False

    @property
    def failed(self) -> bool:
        return self._broken

    async def start(self) -> None:
        async with self._mutex:
            if self._connection is not None:
                raise RuntimeError("Shared supervisor lease already started")
            connection = await self.engine.connect()
            try:
                connection = await connection.execution_options(
                    isolation_level="AUTOCOMMIT",
                )
                backend = await connection.scalar(text("SELECT pg_backend_pid()"))
            except BaseException:
                await connection.invalidate()
                await connection.close()
                raise
            self._connection = connection
            self._backend_pid = int(backend)

    async def _verified_connection(self) -> AsyncConnection:
        connection = self._connection
        if connection is None or self._broken or connection.invalidated:
            self._broken = True
            raise ProjectWorkerBusy("Shared supervisor lease is unavailable")
        try:
            backend = await connection.scalar(text("SELECT pg_backend_pid()"))
        except DBAPIError:
            self._broken = True
            raise ProjectWorkerBusy("Shared supervisor lease connection lost") from None
        if backend != self._backend_pid or connection.invalidated:
            self._broken = True
            raise ProjectWorkerBusy("Shared supervisor lease backend changed")
        return connection

    @asynccontextmanager
    async def project_lock(
        self, project_id: UUID,
    ) -> AsyncIterator[SharedProjectLeaseHandle]:
        key = project_lock_key(project_id)
        async with self._mutex:
            if project_id in self._held:
                raise ProjectWorkerBusy("Project is already held by this supervisor")
            connection = await self._verified_connection()
            try:
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key},
                )
            except DBAPIError:
                self._broken = True
                raise ProjectWorkerBusy("Unable to acquire shared supervisor lease") from None
            except BaseException:
                # An interrupted query might have acquired a session lock
                # before its response reached Python. Retire the session:
                # continuing would strand an untracked lock.
                self._broken = True
                raise
            if not acquired:
                raise ProjectWorkerBusy("Project access worker already running")
            self._held.add(project_id)
        try:
            yield SharedProjectLeaseHandle(self, project_id)
        finally:
            async with self._mutex:
                self._held.discard(project_id)
                if not self._broken and self._connection is not None:
                    try:
                        released = await self._connection.scalar(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": key},
                        )
                        if not released:
                            self._broken = True
                    except DBAPIError:
                        self._broken = True
                    except BaseException:
                        self._broken = True
                        raise

    async def execute_scoped(
        self, project_id: UUID, statement, parameters=None,
    ):
        async with self._mutex:
            if project_id not in self._held:
                raise ProjectWorkerBusy("Project lease was released")
            connection = await self._verified_connection()
            try:
                await connection.execute(
                    text("SELECT set_config('app.current_project_id', :id, false)"),
                    {"id": str(project_id)},
                )
                return await connection.execute(statement, parameters or {})
            except DBAPIError:
                self._broken = True
                raise ProjectWorkerBusy("Shared supervisor lease query failed") from None
            except BaseException:
                # A cancelled scoped query may have changed session context
                # before returning. Force a clean supervisor restart.
                self._broken = True
                raise

    async def confirm(self, project_id: UUID) -> None:
        """Reentrant check with matched unlock: exactly one lease per bot."""
        async with self._mutex:
            if project_id not in self._held:
                raise ProjectWorkerBusy("Project lease was released")
            connection = await self._verified_connection()
            key = project_lock_key(project_id)
            try:
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key},
                )
                if not acquired:
                    self._broken = True
                    raise ProjectWorkerBusy("Shared supervisor lease was lost")
                released = await connection.scalar(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key},
                )
                if not released:
                    self._broken = True
                    raise ProjectWorkerBusy("Shared supervisor lease was lost")
            except DBAPIError:
                self._broken = True
                raise ProjectWorkerBusy("Shared supervisor lease connection lost") from None
            except BaseException:
                self._broken = True
                raise

    async def close(self) -> None:
        async with self._mutex:
            connection = self._connection
            if connection is None:
                return
            self._broken = True
            self._connection = None
            self._backend_pid = None
            # Never return advisory locks to the connection pool.
            try:
                await connection.invalidate()
            finally:
                await connection.close()
