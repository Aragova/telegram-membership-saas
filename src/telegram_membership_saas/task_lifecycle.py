"""Cancellation-resistant cleanup for Telegram pollers and access workers.

A caller may be cancelled repeatedly during rolling restarts, supervisor
reconciliation and shutdown. Cleanup must finish before a PostgreSQL project
lease is released: otherwise an old and new bot can process the same updates.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def complete_cleanup(operation: Awaitable[None]) -> None:
    """Wait for cleanup to finish even if the caller is cancelled again.

    asyncio.shield alone isn't enough: the caller still receives CancelledError.
    Keep re-awaiting the same task instead of cancelling the cleanup itself.
    The original cancellation of the caller will propagate out of its finally.
    """
    cleanup = asyncio.create_task(operation)
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A later cancellation cannot propagate to the cleanup task.
            continue
    await cleanup


def cancel_once(task: asyncio.Task[object] | None) -> None:
    """Do not interrupt a task that is already unwinding its own finally."""
    if task is not None and not task.done() and task.cancelling() == 0:
        task.cancel()
