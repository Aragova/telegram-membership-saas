"""Cancellation-resistant cleanup for Telegram pollers and access workers.

A caller may be cancelled repeatedly during rolling restarts, supervisor
reconciliation and shutdown. Cleanup must finish before a PostgreSQL project
lease is released: otherwise an old and new bot can process the same updates.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Mapping


async def complete_cleanup(operation: Awaitable[None]) -> None:
    """Wait for cleanup to finish even if the caller is cancelled again.

    asyncio.shield alone isn't enough: the caller still receives CancelledError.
    Keep re-awaiting the same task instead of cancelling the cleanup itself.
    The original cancellation of the caller will propagate out of its finally.
    """
    cleanup = asyncio.ensure_future(operation)
    cancelled_during_cleanup = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Keep draining the same child task, then propagate cancellation.
            # This must never release a DB lease before its worker is stopped.
            cancelled_during_cleanup = True
    await cleanup
    if cancelled_during_cleanup:
        raise asyncio.CancelledError


def cancel_once(task: asyncio.Task[object] | None) -> None:
    """Do not interrupt a task that is already unwinding its own finally."""
    if task is not None and not task.done() and task.cancelling() == 0:
        task.cancel()


class RuntimeTaskStopped(RuntimeError):
    """An enabled long-lived runtime task unexpectedly stopped."""


async def wait_for_runtime_tasks(
    polling: asyncio.Task[None],
    background: Mapping[str, asyncio.Task[None]],
) -> None:
    """Observe task termination without cancelling any task implicitly.

    The caller owns cleanup, including draining polling before releasing
    the project lease. Only enabled, indefinitely running tasks belong
    in background. A normal return or unexpected cancellation is also
    a failure for such a task.
    """
    done, _ = await asyncio.wait(
        (polling, *background.values()), return_when=asyncio.FIRST_COMPLETED,
    )
    for name, task in background.items():
        if task not in done:
            continue
        if task.cancelled():
            reason = "cancelled"
        else:
            error = task.exception()
            reason = type(error).__name__ if error is not None else "returned"
        # Do not propagate provider exception contents or token-bearing URLs.
        logging.getLogger(__name__).warning(
            "Runtime background task %s stopped (%s)", name, reason,
        )
        raise RuntimeTaskStopped(f"Runtime background task {name} stopped")
    polling.result()


async def cancel_and_drain(*tasks: asyncio.Task[None] | None) -> None:
    """Request cancellation of all independent tasks before awaiting any."""
    active = tuple(task for task in tasks if task is not None)
    for task in active:
        cancel_once(task)
    await asyncio.gather(*active, return_exceptions=True)
