"""Regression: merchant FK must resolve in a fresh application interpreter.

The main pytest process imports every test module, which can incidentally
register ORM models that a real Telegram worker never imports.
"""

import subprocess
import sys


def test_merchant_fk_registered_without_model_registry_import():
    code = """
from telegram_membership_saas.modules.projects.models import Project

foreign_key = next(iter(Project.__table__.c.merchant_id.foreign_keys))
assert foreign_key.column.table.name == "merchant_identities"
assert foreign_key.column.name == "id"
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stderr
