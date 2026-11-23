"""Cancellation cleanup must be complete before a worker exits.

The cleanup task survives repeated cancel requests; the requester's
CancelledError is propagated only after the child has actually finished.
"""

import asyncio

import pytest

from telegram_membership_saas.task_lifecycle import complete_cleanup


async def test_repeated_cancel_propagates_only_after_cleanup_finishes() -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()
    events: list[str] = []

    async def cleanup() -> None:
        entered.set()
        await finish.wait()
        events.append("cleanup_complete")

    worker = asyncio.create_task(complete_cleanup(cleanup()))
    await asyncio.wait_for(entered.wait(), timeout=2)
    try:
        worker.cancel()
        await asyncio.sleep(0)
        worker.cancel()
        await asyncio.sleep(0)
        assert not worker.done(), "Cancellation cannot abandon protected cleanup"
        assert events == []

        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, timeout=2)
        assert events == ["cleanup_complete"]
    finally:
        finish.set()
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_successful_cleanup_returns_normally() -> None:
    calls: list[str] = []

    async def cleanup() -> None:
        calls.append("complete")

    await complete_cleanup(cleanup())
    assert calls == ["complete"]


async def test_cleanup_failure_is_not_swallowed() -> None:
    async def cleanup() -> None:
        raise RuntimeError("cleanup failure")

    with pytest.raises(RuntimeError, match="cleanup failure"):
        await complete_cleanup(cleanup())
