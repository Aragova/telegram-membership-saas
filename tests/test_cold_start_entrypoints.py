"""Cold-start regression for the staging customer-supervisor entrypoint.

pytest imports many modules before test collection, concealing module-level
cycles that crash a fresh Docker container immediately after deployment.
Each entrypoint must import from a genuinely fresh Python interpreter, with
no database, Redis, Telegram network or settings side effects.
"""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "entrypoint",
    [
        "telegram_membership_saas.customer_supervisor_main",
        "telegram_membership_saas.customer_bot_main",
        "telegram_membership_saas.bot_main",
    ],
)
def test_fresh_process_can_import_telegram_entrypoint(entrypoint: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {entrypoint}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Fresh Python import failed: {entrypoint}\n{result.stderr}"
    )
