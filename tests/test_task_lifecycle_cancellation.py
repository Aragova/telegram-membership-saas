"""Cancellation cleanup must be complete before a worker exits.

The cleanup task survives repeated cancel requests; the requester's
CancelledError is propagated only after the child has actually finished.
"""

import asyncio

import pytest

from telegram_membership_saas.task_lifecycle import (
    RuntimeTaskStopped,
    cancel_and_drain,
    complete_cleanup,
    wait_for_runtime_tasks,
)


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


async def test_cleanup_accepts_existing_future() -> None:
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    await complete_cleanup(future)


@pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
async def test_background_stop_is_detected_without_cancelling_polling(outcome, caplog):
    gate = asyncio.Event()

    async def stopped():
        if outcome == "error":
            raise RuntimeError("secret-transport-payload")
        if outcome == "cancel":
            await gate.wait()

    polling = asyncio.create_task(gate.wait())
    child = asyncio.create_task(stopped())
    if outcome == "cancel":
        child.cancel()
    try:
        with pytest.raises(RuntimeTaskStopped, match="access stopped"):
            await asyncio.wait_for(wait_for_runtime_tasks(
                polling, {"access": child},
            ), timeout=2)
        assert not polling.done()
        assert "secret-transport-payload" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        await cancel_and_drain(polling, child)


async def test_polling_return_and_parent_cancel_do_not_cancel_siblings():
    gate = asyncio.Event()

    async def finish():
        return None

    worker = asyncio.create_task(gate.wait())
    polling = asyncio.create_task(finish())
    try:
        await wait_for_runtime_tasks(polling, {"access": worker})
        assert not worker.done()
        polling = asyncio.create_task(gate.wait())
        watcher = asyncio.create_task(wait_for_runtime_tasks(polling, {"access": worker}))
        await asyncio.sleep(0)
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher
        assert not polling.done()
        assert not worker.done()
    finally:
        await cancel_and_drain(polling, worker)


async def test_independent_tasks_receive_cancel_before_any_drain():
    first_cancelled, second_cancelled = asyncio.Event(), asyncio.Event()

    async def first():
        try:
            await asyncio.Event().wait()
        finally:
            first_cancelled.set()
            await second_cancelled.wait()

    async def second():
        try:
            await asyncio.Event().wait()
        finally:
            second_cancelled.set()
            await first_cancelled.wait()

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await asyncio.sleep(0)
    try:
        await asyncio.wait_for(cancel_and_drain(*tasks, None), timeout=2)
        assert first_cancelled.is_set() and second_cancelled.is_set()
        assert all(task.done() for task in tasks)
    finally:
        first_cancelled.set()
        second_cancelled.set()
        await cancel_and_drain(*tasks)
