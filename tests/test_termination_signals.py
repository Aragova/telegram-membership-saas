"""Exercise real SIGTERM handling, not just direct task.cancel in unit tests."""

import asyncio
import signal
import sys

from telegram_membership_saas.termination_signals import (
    run_with_termination_signals,
)


async def test_actual_sigterm_drains_running_coroutine_in_subprocess():
    script = """
import asyncio
from telegram_membership_saas.termination_signals import run_with_termination_signals

async def runtime():
    print("ready", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0.1)
        print("finished_gracefully", flush=True)

asyncio.run(run_with_termination_signals(runtime()))
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        ready = await asyncio.wait_for(process.stdout.readline(), timeout=8)
        assert ready.strip() == b"ready"
        process.send_signal(signal.SIGTERM)
        out, err = await asyncio.wait_for(process.communicate(), timeout=8)
        assert process.returncode == 0, (out, err)
        assert b"finished_gracefully" in out
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_unrelated_task_cancel_still_propagates():
    ready = asyncio.Event()

    async def runtime():
        ready.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        run_with_termination_signals(runtime())
    )
    await ready.wait()
    task.cancel()
    outcome = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError)
