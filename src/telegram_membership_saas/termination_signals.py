"""Process-level graceful shutdown for central and customer Telegram runtimes.

Aiogram polling runs with handle_signals=False so one process can own many
dispatchers. The top-level process must still translate Docker SIGTERM into
one asyncio cancellation and await all project/dispatcher cleanup.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable

from telegram_membership_saas.task_lifecycle import cancel_once


async def run_with_termination_signals(operation: Awaitable[None]) -> None:
    loop = asyncio.get_running_loop()
    root = asyncio.current_task()
    assert root is not None
    registered: list[signal.Signals] = []
    shutting_down = False

    def request_shutdown() -> None:
        nonlocal shutting_down
        if not shutting_down:
            shutting_down = True
            cancel_once(root)

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Linux Docker containers support loop.add_signal_handler.
            # Failure here must not leave a Telegram process that ignores SIGTERM.
            loop.add_signal_handler(sig, request_shutdown)
            registered.append(sig)
        try:
            await operation
        except asyncio.CancelledError:
            if not shutting_down:
                raise
            # All nested cleanup has completed before the root cancellation
            # emerges from the awaited operation.
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)
